#!/usr/bin/env python3
"""
相席ナビ (AISEKI NAVI) - 画像一括最適化＆軽量化スクリプト
アイキャッチ画像、アイコン、バナー画像を自動でWebP変換・軽量圧縮します。

使い方:
  python3 optimize_images.py                 # static/ 内の全画像を自動最適化
  python3 optimize_images.py <画像パス>     # 指定画像を記事用WebP & サムネイルに自動生成
"""

import os
import sys
from PIL import Image

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

def format_size(bytes_size):
    if bytes_size < 1024:
        return f"{bytes_size} B"
    elif bytes_size < 1024 * 1024:
        return f"{bytes_size / 1024:.1f} KB"
    else:
        return f"{bytes_size / (1024 * 1024):.2f} MB"

def _save_as_jpeg(im, path, quality=85):
    """JPEG cannot store alpha / palette: flatten RGBA/LA/P onto a dark background before saving."""
    if im.mode in ('RGBA', 'LA') or (im.mode == 'P' and 'transparency' in im.info):
        rgba = im.convert('RGBA')
        bg = Image.new('RGB', rgba.size, (17, 17, 19))
        bg.paste(rgba, mask=rgba.split()[-1])
        rgba.close()
        try:
            bg.save(path, 'JPEG', quality=quality, optimize=True)
        finally:
            bg.close()
        return
    if im.mode != 'RGB':
        rgb = im.convert('RGB')
        try:
            rgb.save(path, 'JPEG', quality=quality, optimize=True)
        finally:
            rgb.close()
        return
    im.save(path, 'JPEG', quality=quality, optimize=True)


def optimize_single_eyecatch(image_path, base_name=None):
    """
    1枚のアイキャッチ画像から:
    1. 本文用高画質WebP (1280x720, quality 85)
    2. 一覧用軽量WebPサムネイル (640x360, quality 82)
    3. フォールバック用最適化JPEG
    を一括生成します。
    """
    if not os.path.exists(image_path):
        print(f"Error: {image_path} does not exist.")
        return

    if not base_name:
        filename = os.path.basename(image_path)
        base_name = os.path.splitext(filename)[0]
        if base_name.startswith('thumb-'):
            base_name = base_name.replace('thumb-', '')

    orig_size = os.path.getsize(image_path)
    full_webp_path = os.path.join(STATIC_DIR, f"{base_name}.webp")
    full_jpg_path = os.path.join(STATIC_DIR, f"{base_name}.jpg")
    thumb_webp_path = os.path.join(STATIC_DIR, f"thumb-{base_name}.webp")
    thumb_jpg_path = os.path.join(STATIC_DIR, f"thumb-{base_name}.jpg")

    # Fully load and close the source handle up-front (safe even when the output JPG overwrites the input path)
    with Image.open(image_path) as src:
        src.load()
        im = src.copy()

    try:
        # 1. 本文用 WebP (幅最大1280px)
        im_full = im.copy()
        try:
            if im_full.width > 1280:
                im_full.thumbnail((1280, 720), Image.Resampling.LANCZOS)
            im_full.save(full_webp_path, 'WEBP', quality=85)
            _save_as_jpeg(im_full, full_jpg_path, quality=85)
        finally:
            im_full.close()

        # 2. サムネイル用 WebP & JPG (幅最大640px)
        im_thumb = im.copy()
        try:
            im_thumb.thumbnail((640, 360), Image.Resampling.LANCZOS)
            im_thumb.save(thumb_webp_path, 'WEBP', quality=82)
            _save_as_jpeg(im_thumb, thumb_jpg_path, quality=82)
        finally:
            im_thumb.close()
    finally:
        im.close()

    thumb_size = os.path.getsize(thumb_webp_path)
    saved_ratio = (1 - thumb_size / orig_size) * 100 if orig_size > 0 else 0

    print(f"\n✨ [{base_name}] 最適化完了:")
    print(f"  ・元画像:       {format_size(orig_size)}")
    print(f"  ・本文用 WebP:  {format_size(os.path.getsize(full_webp_path))} ({full_webp_path})")
    print(f"  ・一覧サムネ:   {format_size(thumb_size)} ({thumb_webp_path}) [約{saved_ratio:.0f}% 削減!]")
    print(f"  ・予備用 JPG:   {format_size(os.path.getsize(thumb_jpg_path))}")

def optimize_all_static():
    print("=" * 60)
    print("🚀 相席ナビ static アセット全画像 最適化バッチ開始")
    print("=" * 60)

    # 1. 記事アイキャッチ画像
    eyecatches = [
        ('note-eyecatch.jpg', 'note-eyecatch'),
        ('note-eyecatch-weekday.jpg', 'note-eyecatch-weekday'),
    ]
    for filename, base_name in eyecatches:
        path = os.path.join(STATIC_DIR, filename)
        if os.path.exists(path):
            optimize_single_eyecatch(path, base_name)

    # 2. Xヘッダーバナー
    x_header = os.path.join(STATIC_DIR, 'x-header.jpg')
    if os.path.exists(x_header):
        with Image.open(x_header) as src:
            src.load()
            im = src.copy()
        try:
            webp_path = os.path.join(STATIC_DIR, 'x-header.webp')
            im.save(webp_path, 'WEBP', quality=85)
            _save_as_jpeg(im, x_header, quality=85)
        finally:
            im.close()
        print(f"\n✨ [x-header] 最適化: WebP {format_size(os.path.getsize(webp_path))}, JPG {format_size(os.path.getsize(x_header))}")

    print("\n" + "=" * 60)
    print("🎉 全画像の軽量化・WebP変換が完了しました！")
    print("=" * 60)

if __name__ == '__main__':
    if len(sys.argv) > 1:
        target = sys.argv[1]
        optimize_single_eyecatch(target)
    else:
        optimize_all_static()
