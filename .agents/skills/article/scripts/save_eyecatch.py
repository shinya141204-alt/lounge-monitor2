#!/usr/bin/env python3
"""Resize and optimize a generated 16:9 eyecatch image into static/eyecatch-<slug>.jpg and .webp."""
import os
import sys
from PIL import Image


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 save_eyecatch.py <source_image_path> <slug>")
        sys.exit(1)
    src_path = sys.argv[1]
    slug = sys.argv[2].strip().strip("/")
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
    static_dir = os.path.join(repo_root, "static")
    os.makedirs(static_dir, exist_ok=True)

    im = Image.open(src_path).convert("RGB").resize((1280, 720), Image.Resampling.LANCZOS)
    jpg_out = os.path.join(static_dir, f"eyecatch-{slug}.jpg")
    webp_out = os.path.join(static_dir, f"eyecatch-{slug}.webp")
    im.save(jpg_out, "JPEG", quality=86, optimize=True)
    im.save(webp_out, "WEBP", quality=84)
    print(f"Saved: {jpg_out} ({os.path.getsize(jpg_out)} bytes)")
    print(f"Saved: {webp_out} ({os.path.getsize(webp_out)} bytes)")


if __name__ == "__main__":
    main()
