import re
import os
import json
import argparse
import zipfile
import shutil
from multiprocessing import Pool, cpu_count
from tqdm import tqdm
from typing import List, Dict, Optional
from pathlib import Path
from PyPDF2 import PdfReader
import pdfplumber 

import requests
from urllib.parse import urlparse

def extract_name_from_page(text: str) -> Optional[str]:
    match = re.search(r"\*P(.*?)A(.*?)\*", text)
    return match.group(0) if match else None

def remove_repeated_chars(text: str, threshold: int = 10) -> str:
    pattern = re.compile(r'(.)\1{' + str(threshold - 1) + r',}')
    return pattern.sub('', text)

def clean_text(text: str) -> str:
    text = re.sub(r"\.{3,}", "", text)
    text = text.replace("\n", " ")
    text = remove_repeated_chars(text, threshold=10)
    return text

def remove_do_not_write(entries: Dict[str, str]) -> Dict[str, str]:
    pattern = re.compile(
        r"D\s*O\s*[\W_]*N\s*O\s*T\s*[\W_]*W\s*R\s*I\s*T\s*E\s*[\W_]*I\s*N\s*[\W_]*T\s*H\s*I\s*S\s*[\W_]*A\s*R\s*E\s*A",
        re.IGNORECASE
    )
    cleaned_entries = {}
    for k, v in entries.items():
        new_v = pattern.sub("", v)
        cleaned_entries[k] = new_v
    return cleaned_entries

def process_page(page_number: int, page_text: str, filename: str = "") -> Dict[str, str]:
    cleaned = clean_text(page_text)
    if not cleaned.strip():
        return {}

    if "Questionpaper" in filename:
        name = extract_name_from_page(cleaned)
        if name:
            return {name: cleaned}

    return {f"page_{page_number+1}": cleaned}


def get_tolerances(filename: str) -> tuple:

    base = os.path.basename(filename).lower()
    
    if "questionpaper" in base or "qp" in base:
        return 2, 3
    elif "markscheme" in base or "ms" in base:
        return 5, 6
    elif "examinerreport" in base or "er" in base:
        return 3, 4
    else:
        return 3, 4

def process_pdf(pdf_path: str, x_tolerance: int = 3, y_tolerance: int = 4) -> Dict[str, str]:
    entries = {}
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(tqdm(pdf.pages, desc=os.path.basename(pdf_path), leave=False)):
            text = page.extract_text(x_tolerance=x_tolerance, y_tolerance=y_tolerance) or ""
            result = process_page(i, text, filename=os.path.basename(pdf_path))
            if result:
                entries.update(result)
    entries = remove_do_not_write(entries)
    return entries


def save_json(data: Dict[str, str], out_path: str, link: Optional[str] = None, download_link: Optional[str] = None):
    entries = {}
    if link is not None:
        entries["__source_link__"] = link
    if download_link is not None:
        entries["__download_link__"] = download_link
    entries.update(data)

    # Ensure directory exists
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def merge_json_files(root_dir: str, output_path: str, delete_originals: bool = True) -> int:
    json_files = []
    for dirpath, _, filenames in os.walk(root_dir):
        for f in filenames:
            if f.lower().endswith('.json'):
                full_path = os.path.join(dirpath, f)
                if os.path.abspath(full_path) != os.path.abspath(output_path):
                    json_files.append(full_path)
    if not json_files:
        print("No JSON files found to merge.")
        return 0
    merged_data = {}
    for file_path in json_files:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = json.load(f)
            key = os.path.splitext(os.path.basename(file_path))[0]
            merged_data[key] = content
        except Exception as e:
            print(f"Warning: Could not read {file_path}: {e}")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(merged_data, f, ensure_ascii=False, indent=2)
    count = len(json_files)
    print(f"Merged {count} JSON files into '{output_path}'.")
    if delete_originals:
        for file_path in json_files:
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"Warning: Could not delete {file_path}: {e}")
        print(f"Deleted {count} individual JSON files.")
    return count


def process_single_pdf(pdf_file: str, link: Optional[str] = None, download_link: Optional[str] = None, keep_pdf: bool = True):
    if not os.path.isfile(pdf_file):
        print(f"File {pdf_file} not found. Skipping.")
        return
    x_tol, y_tol = get_tolerances(pdf_file)
    entries = process_pdf(pdf_file, x_tolerance=x_tol, y_tolerance=y_tol)
    
    if not entries:
        print(f"No valid content extracted from {pdf_file}, JSON not created.")
        return
    json_name = os.path.splitext(os.path.basename(pdf_file))[0] + ".json"
    json_path = os.path.join(os.path.dirname(pdf_file), json_name)
    save_json(entries, json_path, link=link, download_link=download_link)
    print(f"Exported to {json_path}")

    if not keep_pdf:
        try:
            os.remove(pdf_file)
            print(f"Deleted {pdf_file}")
        except Exception as e:
            print(f"Failed to delete {pdf_file}: {e}")

def process_zip(zip_file: str, link: Optional[str] = None, download_link: Optional[str] = None, keep_pdf: bool = True, keep_extracted: bool = False):
    if not os.path.isfile(zip_file):
        print(f"File {zip_file} not found. Skipping.")
        return

    # Create extraction directory in the same location as the zip file
    zip_dir = os.path.dirname(zip_file)
    zip_basename = os.path.splitext(os.path.basename(zip_file))[0]
    extract_dir = os.path.join(zip_dir, zip_basename)

    try:
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(zip_file, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
    except Exception as e:
        print(f"Failed to extract zip: {e}")
        return

    pdf_paths = []
    for root, dirs, files in os.walk(extract_dir):
        for fname in files:
            if fname.lower().endswith('.pdf'):
                pdf_paths.append(os.path.join(root, fname))

    if not pdf_paths:
        print("No PDF files found in zip!")
        return

    for pdf_path in pdf_paths:
        x_tol, y_tol = get_tolerances(pdf_path)
        entries = process_pdf(pdf_path, x_tolerance=x_tol, y_tolerance=y_tol) 
        if not entries:
            print(f"No valid content extracted from {pdf_path} in zip, JSON not created.")
            continue
        json_name = os.path.splitext(os.path.basename(pdf_path))[0] + ".json"
        json_path = os.path.join(os.path.dirname(pdf_path), json_name)
        save_json(entries, json_path, link=link, download_link=download_link)
        print(f"Exported to {json_path}")

        if not keep_pdf:
            try:
                os.remove(pdf_path)
                print(f"Deleted {pdf_path}")
            except Exception as e:
                print(f"Failed to delete {pdf_path}: {e}")

    if not keep_extracted:
        try:
            shutil.rmtree(extract_dir)
            print(f"Cleaned up extracted directory: {extract_dir}")
        except Exception as e:
            print(f"Failed to clean up extracted directory: {e}")

def find_all_pdfs(root_dir: str) -> List[str]:
    """
    Recursively find all PDF files in all directories and subdirectories under root_dir
    that do not already have a corresponding .json file in the same folder.
    """
    pdfs_to_process = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        pdfs = [f for f in filenames if f.lower().endswith('.pdf')]
        jsons = set(os.path.splitext(f)[0] for f in filenames if f.lower().endswith('.json'))
        for pdf in pdfs:
            pdf_name = os.path.splitext(pdf)[0]
            if pdf_name not in jsons:
                pdfs_to_process.append(os.path.join(dirpath, pdf))
    return pdfs_to_process

def download_file(url: str, target_dir: str) -> Optional[str]:
    os.makedirs(target_dir, exist_ok=True)
    parsed_url = urlparse(url)
    file_name = os.path.basename(parsed_url.path)

    if not file_name:
        file_name = "downloaded_file"

    local_path = os.path.join(target_dir, file_name)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/zip,application/pdf,application/octet-stream,*/*"
    }

    try:
        with requests.Session() as session:
            response = session.get(url, headers=headers, stream=True, allow_redirects=True, timeout=120)
            response.raise_for_status()
            ctype = response.headers.get('Content-Type', '').lower()
            if file_name.lower().endswith('.zip') and 'zip' not in ctype:
                print(f"Warning: Content-Type is '{ctype}', expected ZIP.")

            with open(local_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

        # Validate file signature
        with open(local_path, 'rb') as f:
            first_bytes = f.read(512)
            if file_name.lower().endswith('.zip') and not first_bytes.startswith(b'PK\x03\x04'):
                try:
                    text = first_bytes.decode("utf-8", errors="ignore")
                    if "<html" in text.lower():
                        print("ERROR: Downloaded file is an HTML page, not a ZIP. You may need to authenticate or use the correct download link.")
                        os.remove(local_path)
                        return None
                except Exception:
                    pass
                print("WARNING: ZIP file signature not found. File may be corrupted or incomplete.")
            elif file_name.lower().endswith('.pdf') and not first_bytes.startswith(b'%PDF'):
                print("WARNING: PDF file signature not found. File may be corrupted or incomplete.")

        print(f"File downloaded to {local_path}")
        return local_path
    except Exception as e:
        print(f"Failed to download {url}: {e}")
        return None

def process_links_from_file(txt_path: str, download_dir: str, keep_pdf: bool = True, keep_extracted: bool = False):
    if not os.path.isfile(txt_path):
        print(f"Links file '{txt_path}' not found.")
        return

    with open(txt_path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    if len(lines) % 2 != 0:
        print("Links file does not have an even number of lines (source/download pairs required).")
        return

    for idx in range(0, len(lines), 2):
        source_link = lines[idx]
        download_link = lines[idx+1]
        print(f"\nProcessing pair:\n  Source link: {source_link}\n  Download link: {download_link}")
        file_path = download_file(source_link, download_dir)
        if not file_path:
            print(f"Failed to download from: {source_link}")
            continue

        if file_path.lower().endswith('.pdf'):
            process_single_pdf(file_path, link=source_link, download_link=download_link, keep_pdf=keep_pdf)
        elif file_path.lower().endswith('.zip'):
            process_zip(file_path, link=source_link, download_link=download_link, keep_pdf=keep_pdf, keep_extracted=keep_extracted)
        else:
            print(f"Unsupported file type for: {file_path}")

def prompt_for_links_and_process(download_dir: str, keep_pdf: bool = True, keep_extracted: bool = False):
    print("\n--- Manual Entry Mode ---")
    print("Enter source and download links below. Leave source link blank and press Enter to stop.")
    while True:
        source_link = input("\nEnter SOURCE link (leave blank to stop): ").strip()
        if not source_link:
            print("Stopping input loop.")
            break
        download_link = input("Enter DOWNLOAD link/text: ").strip()
        if not download_link:
            print("No download link provided, skipping this pair.")
            continue
        file_path = download_file(source_link, download_dir)
        if not file_path:
            print(f"Failed to download from: {source_link}")
            continue

        if file_path.lower().endswith('.pdf'):
            process_single_pdf(file_path, link=source_link, download_link=download_link, keep_pdf=keep_pdf)
        elif file_path.lower().endswith('.zip'):
            process_zip(file_path, link=source_link, download_link=download_link, keep_pdf=keep_pdf, keep_extracted=keep_extracted)
        else:
            print(f"Unsupported file type for: {file_path}")

def _process_pdf_wrapper(args: tuple):
    """Wrapper function for multiprocessing to handle multiple arguments."""
    pdf_file, link, download_link, keep_pdf = args
    process_single_pdf(pdf_file, link=link, download_link=download_link, keep_pdf=keep_pdf)

def main():
    parser = argparse.ArgumentParser(
        description="PDF/ZIP processor: can process a list from text file, prompt interactively, or process all PDFs in all folders."
    )
    parser.add_argument('--text_links', type=str, default=None, help='Text file with alternating source/download links (line1=source, line2=download, ...)')
    parser.add_argument('--dir', type=str, default="downloads", help='Directory to save downloaded files')
    parser.add_argument('--prompt', action='store_true', help='Prompt for source/download links interactively')
    parser.add_argument('--keep-pdf', action='store_true', default=False, help='Keep PDF files after processing (default: delete)')
    parser.add_argument('--keep-extracted', action='store_true', default=False, help='Keep extracted directories from ZIP files (default: delete)')
    args = parser.parse_args()
    
    # 1. Process from text file if given
    if args.text_links:
        process_links_from_file(args.text_links, args.dir, keep_pdf=args.keep_pdf, keep_extracted=args.keep_extracted)
        return

    # 2. Prompt the user for links if requested
    if args.prompt:
        prompt_for_links_and_process(args.dir, keep_pdf=args.keep_pdf, keep_extracted=args.keep_extracted)
        return

    # 3. Default: process all PDFs in all subdirectories, prompt for download link/text for each batch
    current_dir = os.getcwd()
    pdf_files = find_all_pdfs(current_dir)
    if not pdf_files:
        print("No new PDF files found in any subdirectory.")
        return

    print(f"Found {len(pdf_files)} PDF(s) in all folders and subfolders.")
    try:
        download_link = input("Enter the download link or text to store as __download_link__ in the resulting JSON files (or leave blank for none): ").strip()
        if not download_link:
            download_link = None
    except Exception:
        download_link = None

    if len(pdf_files) == 1:
        process_single_pdf(pdf_files[0], download_link=download_link, keep_pdf=args.keep_pdf)
    else:
        # Prepare arguments for multiprocessing
        process_args = [(pdf, None, download_link, args.keep_pdf) for pdf in pdf_files]

        with Pool(processes=min(cpu_count(), len(pdf_files))) as file_pool:
            list(tqdm(
                file_pool.imap_unordered(_process_pdf_wrapper, process_args),
                total=len(pdf_files),
                desc="PDFs"
            ))
    
    print("\nStarting merge process...")
    merge_json_files(current_dir, os.path.join(current_dir, "merged.json"))
if __name__ == "__main__":
    main()
