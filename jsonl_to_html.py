#!/usr/bin/env python3
"""
Convert Reddit JSONL format (Reddit API JSON) to HTML visualization format.
Reads JSONL files and converts them to HTML visualization format.
Media-aware: Downloads all images and videos from posts and comments.
"""

import json
import html as html_lib
import os
import hashlib
import requests
import re
import time
import argparse
import glob
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock


def extract_media_urls_from_text(text):
    """Extract media URLs from text using regex patterns, including giphy links"""
    if not text:
        return []
    
    urls = []
    patterns = [
        r'https?://(?:preview\.|i\.|v\.)?redd\.it/[^\s<>")\]]+',
        r'https?://[^\s<>")\]]+\.(?:jpg|jpeg|png|gif|webp|svg|bmp|ico)',
        r'https?://[^\s<>")\]]+\.(?:mp4|avi|mov|wmv|flv|webm|m4v|mpg|mpeg)',
        r'https?://(?:i\.)?imgur\.com/[^\s<>")\]]+',
    ]
    
    found_urls = set()
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            match = match.rstrip('.,;:!?)')
            if match not in found_urls:
                found_urls.add(match)
                urls.append(match)
    
    # Extract giphy links: ![gif](giphy|ID) or [gif](giphy|ID)
    giphy_pattern = r'(?:!?\[[^\]]*\]\()?giphy\|([A-Za-z0-9]+)\)?'
    giphy_matches = re.findall(giphy_pattern, text, re.IGNORECASE)
    for giphy_id in giphy_matches:
        # Convert giphy ID to URL
        # Try different giphy URL formats
        giphy_urls = [
            f'https://media.giphy.com/media/{giphy_id}/giphy.gif',
            f'https://i.giphy.com/{giphy_id}.gif',
            f'https://media.giphy.com/media/{giphy_id}/200.gif',
        ]
        # Add the most common format
        if f'giphy|{giphy_id}' not in found_urls:
            found_urls.add(f'giphy|{giphy_id}')
            urls.append(giphy_urls[0])  # Use the first format, we'll try others if this fails
    
    return urls


def extract_media_from_reddit_post(post_data):
    """Extract all media URLs from a Reddit post data structure"""
    media_urls = set()
    
    # Direct URL field (can be image/video URLs)
    if 'url' in post_data:
        url = post_data['url']
        if url and isinstance(url, str) and url.startswith('http'):
            # Skip non-media URLs (reddit.com links, etc.)
            if any(domain in url for domain in ['redd.it', 'imgur.com', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.mp4', '.webm']):
                media_urls.add(url)
    
    # URL override (often the actual media URL)
    if 'url_overridden_by_dest' in post_data:
        url = post_data['url_overridden_by_dest']
        if url and isinstance(url, str) and url.startswith('http'):
            if any(domain in url for domain in ['redd.it', 'imgur.com', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.mp4', '.webm']):
                media_urls.add(url)
    
    # Thumbnail
    if 'thumbnail' in post_data:
        thumb = post_data['thumbnail']
        if thumb and isinstance(thumb, str) and thumb.startswith('http'):
            media_urls.add(thumb)
    
    # Preview images
    if 'preview' in post_data and isinstance(post_data['preview'], dict):
        images = post_data['preview'].get('images', [])
        for img in images:
            if isinstance(img, dict):
                # Source image
                if 'source' in img and isinstance(img['source'], dict):
                    src_url = img['source'].get('url')
                    if src_url and isinstance(src_url, str) and src_url.startswith('http'):
                        # Decode HTML entities in URL
                        src_url = html_lib.unescape(src_url)
                        media_urls.add(src_url)
                # Variants (higher res versions)
                if 'variants' in img and isinstance(img['variants'], dict):
                    for variant in img['variants'].values():
                        if isinstance(variant, dict) and 'source' in variant:
                            var_url = variant['source'].get('url')
                            if var_url and isinstance(var_url, str) and var_url.startswith('http'):
                                var_url = html_lib.unescape(var_url)
                                media_urls.add(var_url)
    
    # Gallery data
    if 'gallery_data' in post_data and 'media_metadata' in post_data:
        gallery_data = post_data.get('gallery_data', {})
        media_metadata = post_data.get('media_metadata', {})
        items = gallery_data.get('items', [])
        for item in items:
            media_id = item.get('media_id')
            if media_id and media_id in media_metadata:
                media_info = media_metadata[media_id]
                if 's' in media_info and isinstance(media_info['s'], dict):
                    img_url = media_info['s'].get('u')
                    if img_url and isinstance(img_url, str) and img_url.startswith('http'):
                        img_url = html_lib.unescape(img_url)
                        media_urls.add(img_url)
    
    # Extract from selftext body
    if 'selftext' in post_data:
        body_urls = extract_media_urls_from_text(post_data['selftext'])
        media_urls.update(body_urls)
    
    return list(media_urls)


def extract_media_from_comment(comment_data):
    """Extract media URLs from a comment"""
    media_urls = []
    
    if 'body' in comment_data:
        body_urls = extract_media_urls_from_text(comment_data['body'])
        media_urls.extend(body_urls)
    
    return media_urls


def get_local_path_for_url(url, media_dir):
    """Generate local path for a URL without downloading"""
    url_decoded = html_lib.unescape(url)
    url_hash = hashlib.md5(url_decoded.encode()).hexdigest()[:16]
    
    parsed_url = urlparse(url_decoded)
    path = parsed_url.path
    ext = os.path.splitext(path)[1].lower()
    
    if not ext or ext not in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.mp4', '.webm']:
        query_params = parse_qs(parsed_url.query)
        if 'format' in query_params:
            format_hint = query_params['format'][0].lower()
            ext = {'png': '.png', 'gif': '.gif', 'webp': '.webp'}.get(format_hint, '.jpg')
            if format_hint in ['jpg', 'jpeg', 'pjpg', 'pjpeg']:
                ext = '.jpg'
        else:
            if 'preview.redd.it' in url or 'i.redd.it' in url:
                if '.png' in path:
                    ext = '.png'
                elif '.gif' in path:
                    ext = '.gif'
                else:
                    ext = '.jpg'
            else:
                ext = '.jpg'
    
    local_filename = f"{url_hash}{ext}"
    return os.path.join(media_dir, local_filename)


def download_media(url, media_dir, url_to_local_path, lock):
    """Download media from URL and return local path"""
    url_decoded = html_lib.unescape(url)
    
    with lock:
        if url_decoded in url_to_local_path:
            return url_to_local_path[url_decoded]
        if url in url_to_local_path:
            return url_to_local_path[url]
    
    local_path = get_local_path_for_url(url, media_dir)
    
    if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
        with lock:
            url_to_local_path[url_decoded] = local_path
            url_to_local_path[url] = local_path
        return local_path
    
    try:
        url_to_fetch = url_decoded
        max_retries = 2
        
        for attempt in range(max_retries):
            try:
                if attempt == 0:
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                        'Accept': 'image/webp,image/*,*/*;q=0.8',
                        'Referer': 'https://www.reddit.com/',
                    }
                else:
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0',
                    }
                
                response = requests.get(
                    url_to_fetch,
                    headers=headers,
                    timeout=15,
                    stream=True,
                    allow_redirects=True
                )
                response.raise_for_status()
                
                with open(local_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                
                if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                    with lock:
                        url_to_local_path[url_to_fetch] = local_path
                        url_to_local_path[url] = local_path
                    return local_path
                else:
                    if os.path.exists(local_path):
                        os.remove(local_path)
                    continue
                    
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    time.sleep(0.5)
                    continue
                else:
                    raise
        
        return None
        
    except Exception as e:
        return None


def download_media_batch(urls, media_dir, url_to_local_path, max_workers=50):
    """Download multiple URLs in parallel"""
    unique_urls = list(set(urls))
    if not unique_urls:
        return {}
    
    print(f"\nDownloading {len(unique_urls)} unique media files using {max_workers} workers...")
    
    results = {}
    completed = 0
    start_time = time.time()
    lock = Lock()
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url = {executor.submit(download_media, url, media_dir, url_to_local_path, lock): url for url in unique_urls}
        
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                local_path = future.result()
                results[url] = local_path
                completed += 1
                
                if completed % 10 == 0 or completed == len(unique_urls):
                    elapsed = time.time() - start_time
                    rate = completed / elapsed if elapsed > 0 else 0
                    print(f"Progress: {completed}/{len(unique_urls)} ({completed/len(unique_urls)*100:.1f}%) - {rate:.1f} files/sec")
            except Exception as e:
                results[url] = None
                completed += 1
    
    elapsed = time.time() - start_time
    successful = sum(1 for v in results.values() if v is not None)
    print(f"✓ Downloaded {successful}/{len(unique_urls)} files in {elapsed:.1f} seconds ({successful/elapsed:.1f} files/sec)" if elapsed > 0 else f"✓ Downloaded {successful}/{len(unique_urls)} files")
    
    return results


def replace_urls_in_text(text, url_replacements):
    """Replace URLs in text with local paths, including giphy links"""
    if not text or not url_replacements:
        return text
    
    text = str(text)
    
    # First, replace giphy|ID patterns with local paths if they were downloaded
    # Check if any giphy URLs were downloaded
    # Pattern matches: ![gif](giphy|ID) or [gif](giphy|ID) or just giphy|ID
    giphy_pattern = r'(!?\[[^\]]*\]\()?giphy\|([A-Za-z0-9]+)(\))?'
    def replace_giphy_with_local(match):
        giphy_id = match.group(2)
        # Check if this giphy was downloaded
        giphy_url = f'https://media.giphy.com/media/{giphy_id}/giphy.gif'
        if giphy_url in url_replacements:
            local_path = url_replacements[giphy_url]
            if local_path:
                # If it's in markdown format ![text](giphy|ID), replace with just the local path
                # The embed_media_in_text will convert it to an img tag
                if match.group(1):  # Has markdown prefix like ![gif](
                    return local_path  # Just return the path, drop the markdown wrapper
                else:
                    # Just the giphy|ID pattern
                    return local_path
        # If not downloaded, keep the original pattern (it will be handled by embed_media_in_text)
        return match.group(0)
    
    text = re.sub(giphy_pattern, replace_giphy_with_local, text, flags=re.IGNORECASE)
    
    if 'http' not in text:
        return text
    
    for original_url, local_path in url_replacements.items():
        if local_path and original_url in text:
            text = text.replace(original_url, local_path)
        
        decoded_url = html_lib.unescape(original_url)
        if decoded_url != original_url and decoded_url in text:
            text = text.replace(decoded_url, local_path)
    
    return text


def embed_media_in_text(text, media_dir='downloaded_media'):
    """Convert local media file paths in text to HTML img/video tags, and handle giphy links"""
    if not text:
        return text
    
    text = str(text)
    
    # Convert remaining giphy links to image tags (if not already replaced with local paths)
    # Pattern: ![gif](giphy|ID) or [gif](giphy|ID) or just giphy|ID
    # Note: If giphy was downloaded, replace_urls_in_text should have already replaced it with a local path
    giphy_pattern = r'(?:!?\[[^\]]*\]\()?giphy\|([A-Za-z0-9]+)\)?'
    def replace_giphy(match):
        giphy_id = match.group(1)
        # Use the direct URL (if it wasn't downloaded, this is the fallback)
        giphy_url = f'https://media.giphy.com/media/{giphy_id}/giphy.gif'
        return f'<img src="{giphy_url}" style="max-width: 100%; height: auto; margin: 10px 0; border-radius: 4px;" alt="GIF" />'
    
    text = re.sub(giphy_pattern, replace_giphy, text, flags=re.IGNORECASE)
    
    # Extract just the directory name (not full path) for pattern matching
    # Paths in text are relative like "downloaded_media/filename.jpg"
    # But media_dir might be a full path like "output/downloaded_media"
    media_dir_name = os.path.basename(str(media_dir))
    
    # Pattern to match local media paths - match paths that start with the directory name
    # This will match paths like "downloaded_media/filename.jpg" 
    # Also match standalone filenames if they're in the media directory
    media_pattern = rf'{re.escape(media_dir_name)}/[^\s<>")\]]+\.(jpg|jpeg|png|gif|webp|svg|bmp|ico|mp4|webm|avi|mov|wmv|flv|m4v|mpg|mpeg)'
    
    def replace_media_path(match):
        media_path = match.group(0)  # Get the full matched path
        ext = os.path.splitext(media_path)[1].lower()
        
        # Escape the path for HTML
        escaped_path = html_lib.escape(media_path)
        
        # Convert to image tag for images
        if ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp', '.ico']:
            return f'<img src="{escaped_path}" style="max-width: 100%; height: auto; margin: 10px 0; border-radius: 4px;" alt="Image" />'
        # Convert to video tag for videos
        elif ext in ['.mp4', '.webm', '.avi', '.mov', '.wmv', '.flv', '.m4v', '.mpg', '.mpeg']:
            return f'<video controls style="max-width: 100%; margin: 10px 0; border-radius: 4px;"><source src="{escaped_path}" type="video/{ext[1:]}">Your browser does not support the video tag.</video>'
        else:
            return media_path
    
    # Replace media paths with HTML tags
    text = re.sub(media_pattern, replace_media_path, text, flags=re.IGNORECASE)
    
    return text


def parse_reddit_comment(comment_data, url_replacements=None, media_dir='downloaded_media'):
    """Parse a Reddit comment (kind='t1') and extract its data"""
    if comment_data.get('kind') != 't1':
        return None
    
    data = comment_data.get('data', {})
    comment_id = data.get('name', '')
    author = data.get('author', '[deleted]')
    body = data.get('body', '')
    score = data.get('score', 0)
    created_utc = data.get('created_utc', 0)
    
    # Replace URLs in body text
    if url_replacements:
        body = replace_urls_in_text(body, url_replacements)
    
    # Embed media (convert local paths to HTML img/video tags)
    body = embed_media_in_text(body, media_dir)
    
    # Parse replies recursively
    replies = []
    if 'rep
