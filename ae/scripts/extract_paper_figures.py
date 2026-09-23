#!/usr/bin/env python3
"""Render the supplied ATC 2026 paper's original figures and tables for the AE README.

Author-side documentation utility, requiring pypdfium2 and Pillow. Reviewers do
not need these dependencies: the PNGs and their provenance ship in the repository.
"""
import argparse
import hashlib
import json
from pathlib import Path

AE = Path(__file__).resolve().parents[1]
PAPER_SHA256 = '1cc012a6ba4afdd127372a236333983ad6d17934ac63680b37faa3e6f65bb95e'
# One-based PDF page, followed by a rectangle in points from the top-left.
# Each rectangle includes the original caption; no data or labels are redrawn.
REGIONS = {
    'figure-01': (2, (315, 69, 562, 239)),
    'figure-02': (4, (50, 232, 298, 410)),
    'figure-03': (5, (50, 69, 562, 250)),
    'figure-04': (6, (50, 69, 298, 253)),
    'figure-05': (7, (50, 69, 298, 321)),
    'figure-06': (8, (50, 69, 298, 222)),
    'figure-07': (10, (315, 69, 563, 251)),
    'figure-08': (11, (50, 211, 562, 378)),
    'figure-09': (12, (50, 69, 298, 286)),
    'table-01': (4, (50, 69, 562, 230)),
    'table-02': (11, (50, 69, 562, 209)),
    'table-03': (11, (315, 381, 562, 569)),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf', type=Path, default=AE / 'reference/paper158.pdf')
    parser.add_argument('--output', type=Path, default=AE / 'reference/figures')
    args = parser.parse_args()
    digest = hashlib.sha256(args.pdf.read_bytes()).hexdigest()
    if digest != PAPER_SHA256:
        parser.error('PDF differs from atc26-paper158.pdf; recheck page numbers and crop rectangles before updating them')
    import pypdfium2 as pdfium

    args.output.mkdir(parents=True, exist_ok=True)
    artifacts = []
    with pdfium.PdfDocument(args.pdf) as pdf:
        for name, (number, bbox) in REGIONS.items():
            page = pdf[number - 1]
            width, height = page.get_size()
            left, top, right, bottom = bbox
            bitmap = page.render(scale=4, crop=(left, height - bottom, width - right, top))
            picture = bitmap.to_pil()
            destination = args.output / (name + '.png')
            picture.save(destination)
            artifacts.append(dict(file=destination.name, pdf_page=number, crop_points=list(bbox),
                                  width=picture.width, height=picture.height,
                                  sha256=hashlib.sha256(destination.read_bytes()).hexdigest()))
            bitmap.close()
            page.close()
    manifest = dict(source='atc26-paper158.pdf', repository_copy='ae/reference/paper158.pdf',
                    source_sha256=digest, dpi=288, coordinate_system='PDF points from top-left',
                    content='Original paper figures and tables, including captions; not AE measurements',
                    artifacts=artifacts)
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Extracted {len(artifacts)} paper figures/tables to {args.output}')


if __name__ == '__main__':
    main()
