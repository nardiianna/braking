#!/usr/bin/env python3
"""
Carica in bulk le immagini prodotto su Storeden via API, associandole per SKU.

Setup:
  1. Crea un file .env in questa cartella con:
       STOREDEN_KEY=xxxxx
       STOREDEN_EXCHANGE=xxxxx
  2. Esporta da Storeden un CSV con almeno le colonne SKU e ID prodotto
     (i nomi delle colonne si passano con --sku-col e --uid-col).
  3. Metti le immagini in una cartella, nominate come <SKU>.jpg (o .png, ecc).

Uso:
  python3 upload_images.py --images-dir /percorso/immagini --mapping-csv mapping.csv
  python3 upload_images.py --images-dir /percorso/immagini --mapping-csv mapping.csv --limit 10 --dry-run
"""
import argparse
import base64
import csv
import hashlib
import json
import mimetypes
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_URL = "https://connect.storeden.com/v1.1/products/image.json"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
HI_RES_HINTS = ("alta ris", "hires", "hi-res", "hi res")
LO_RES_HINTS = ("bassa ris", "lowres", "low-res", "low res")


def file_hash(path):
    return hashlib.md5(path.read_bytes()).hexdigest()


def collect_images(images_dir, exclude_dirs):
    exclude_lower = {d.lower() for d in exclude_dirs}
    all_files = [
        p
        for p in images_dir.rglob("*")
        if p.suffix.lower() in IMAGE_EXTENSIONS
        and not exclude_lower.intersection(part.lower() for part in p.relative_to(images_dir).parts[:-1])
    ]

    by_sku = {}
    for p in all_files:
        by_sku.setdefault(p.stem.strip().upper(), []).append(p)

    chosen = {}
    ambiguous = {}
    for sku, paths in by_sku.items():
        if len(paths) == 1:
            chosen[sku] = paths[0]
            continue

        lower_paths = [str(p).lower() for p in paths]
        hi = [p for p, lp in zip(paths, lower_paths) if any(h in lp for h in HI_RES_HINTS)]
        lo = [p for p, lp in zip(paths, lower_paths) if any(h in lp for h in LO_RES_HINTS)]

        if len(hi) == 1 and len(paths) - len(hi) == len(lo):
            chosen[sku] = hi[0]
            continue

        hashes = {file_hash(p) for p in paths}
        if len(hashes) == 1:
            chosen[sku] = sorted(paths)[0]
            continue

        ambiguous[sku] = paths

    return chosen, ambiguous


def load_env(env_path):
    values = {}
    if not env_path.exists():
        sys.exit(f"File .env non trovato: {env_path}")
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    missing = [k for k in ("STOREDEN_KEY", "STOREDEN_EXCHANGE") if k not in values]
    if missing:
        sys.exit(f"Mancano nel .env: {', '.join(missing)}")
    return values["STOREDEN_KEY"], values["STOREDEN_EXCHANGE"]


def sniff_delimiter(csv_path):
    with open(csv_path, encoding="utf-8-sig") as f:
        sample = f.read(4096)
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,\t").delimiter
    except csv.Error:
        return ","


def extract_uid(raw_value):
    raw_value = (raw_value or "").strip()
    if raw_value.isdigit():
        return raw_value
    match = re.search(r"/product/(\d+)/", raw_value)
    if match:
        return match.group(1)
    match = re.search(r"(\d+)", raw_value)
    return match.group(1) if match else ""


def load_mapping(csv_path, sku_col, uid_col, delimiter=None):
    mapping = {}
    delimiter = delimiter or sniff_delimiter(csv_path)
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        if sku_col not in reader.fieldnames or uid_col not in reader.fieldnames:
            sys.exit(
                f"Colonne non trovate nel CSV. Colonne disponibili: {reader.fieldnames}\n"
                f"Attese: --sku-col={sku_col!r} --uid-col={uid_col!r} (delimiter usato: {delimiter!r})"
            )
        for row in reader:
            sku = (row.get(sku_col) or "").strip().upper()
            uid = extract_uid(row.get(uid_col))
            if sku and uid:
                mapping[sku] = uid
    return mapping


def call_api(key, exchange, uid, b64_data, related=0, retries=3):
    payload = urllib.parse.urlencode(
        {"uid": uid, "base64": b64_data, "related": related}
    ).encode("utf-8")
    req = urllib.request.Request(API_URL, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("key", key)
    req.add_header("exchange", exchange)

    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8")
                return True, body
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 509) and attempt < retries:
                time.sleep(2 * attempt)
                continue
            return False, f"HTTP {e.code}: {body}"
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            return False, f"Errore di rete: {e.reason}"
    return False, "Numero massimo di tentativi superato"


def main():
    parser = argparse.ArgumentParser(description="Upload bulk immagini prodotto Storeden per SKU")
    parser.add_argument("--images-dir", required=True, help="Cartella con le immagini (nome file = SKU)")
    parser.add_argument("--mapping-csv", required=True, help="CSV con colonne SKU e ID prodotto")
    parser.add_argument("--sku-col", default="sku", help="Nome colonna SKU nel CSV (default: sku)")
    parser.add_argument(
        "--uid-col",
        default="path",
        help="Nome colonna da cui estrarre l'ID prodotto (default: path, es. /product/41126660/...)",
    )
    parser.add_argument("--delimiter", default=None, help="Delimitatore CSV (default: auto-rilevato, es. ';')")
    parser.add_argument("--env-file", default=".env", help="Percorso del file .env (default: ./.env)")
    parser.add_argument("--related", type=int, default=0, help="0 = immagine principale, 1 = correlata")
    parser.add_argument("--limit", type=int, default=None, help="Carica solo le prime N immagini (per test)")
    parser.add_argument("--offset", type=int, default=0, help="Salta le prime N immagini (per evitare doppioni su test gia' fatti)")
    parser.add_argument("--append", action="store_true", help="Aggiunge al report esistente invece di sovrascriverlo (per riprendere una corsa interrotta)")
    parser.add_argument("--dry-run", action="store_true", help="Non chiama l'API, mostra solo cosa farebbe")
    parser.add_argument("--delay", type=float, default=0.3, help="Pausa in secondi tra una chiamata e l'altra")
    parser.add_argument("--report", default="report.csv", help="File CSV di output con l'esito di ogni upload")
    parser.add_argument(
        "--exclude-dir",
        action="append",
        default=["OLD"],
        help="Nome di cartella da escludere dalla scansione (ripetibile, default: OLD)",
    )
    parser.add_argument(
        "--ambiguous-report",
        default="ambigui.csv",
        help="File CSV con gli SKU che hanno piu' file candidati diversi (non caricati)",
    )
    parser.add_argument(
        "--missing-report",
        default="sku_senza_immagine.csv",
        help="File CSV con gli SKU del catalogo senza nessuna immagine corrispondente",
    )
    args = parser.parse_args()

    images_dir = Path(args.images_dir)
    if not images_dir.is_dir():
        sys.exit(f"Cartella immagini non trovata: {images_dir}")

    key = exchange = None
    if not args.dry_run:
        key, exchange = load_env(Path(args.env_file))

    mapping = load_mapping(args.mapping_csv, args.sku_col, args.uid_col, args.delimiter)
    print(f"Mapping caricato: {len(mapping)} SKU trovati nel CSV")

    chosen, ambiguous = collect_images(images_dir, args.exclude_dir)
    print(f"SKU con immagine risolta senza ambiguita': {len(chosen)}")
    print(f"SKU con file candidati ambigui (esclusi da questo giro): {len(ambiguous)}")

    with open(args.ambiguous_report, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["sku", "file_candidati"])
        for sku, paths in sorted(ambiguous.items()):
            writer.writerow([sku, " | ".join(str(p) for p in paths)])

    missing_skus = sorted(set(mapping) - set(chosen))
    with open(args.missing_report, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["sku"])
        for sku in missing_skus:
            writer.writerow([sku])
    print(f"SKU del catalogo senza nessuna immagine: {len(missing_skus)} (vedi {args.missing_report})")

    to_upload = sorted((sku, path) for sku, path in chosen.items() if sku in mapping)
    if args.offset:
        to_upload = to_upload[args.offset :]
    if args.limit:
        to_upload = to_upload[: args.limit]
    print(f"Immagini da caricare in questo giro: {len(to_upload)}")

    ok_count = 0
    fail_count = 0

    report_path = Path(args.report)
    write_header = not (args.append and report_path.exists())
    mode = "a" if args.append else "w"

    with open(report_path, mode, newline="", encoding="utf-8") as report_f:
        writer = csv.writer(report_f)
        if write_header:
            writer.writerow(["file", "sku", "uid", "status", "message"])
            report_f.flush()

        for i, (sku, img_path) in enumerate(to_upload, start=1):
            uid = mapping[sku]

            if args.dry_run:
                writer.writerow([str(img_path), sku, uid, "DRY_RUN_OK", ""])
                report_f.flush()
                ok_count += 1
                print(f"[{i}/{len(to_upload)}] {img_path} -> SKU {sku} -> uid {uid} (dry-run)")
                continue

            data = img_path.read_bytes()
            b64_data = base64.b64encode(data).decode("ascii")

            success, message = call_api(key, exchange, uid, b64_data, related=args.related)
            status = "OK" if success else "ERRORE"
            writer.writerow([str(img_path), sku, uid, status, message])
            report_f.flush()
            if success:
                ok_count += 1
                print(f"[{i}/{len(to_upload)}] {img_path} -> SKU {sku} -> uid {uid}: OK")
            else:
                fail_count += 1
                print(f"[{i}/{len(to_upload)}] {img_path} -> SKU {sku} -> uid {uid}: ERRORE - {message}")

            time.sleep(args.delay)

    print(f"\nCompletato. OK: {ok_count}  Falliti: {fail_count}  Report: {args.report}")


if __name__ == "__main__":
    main()
