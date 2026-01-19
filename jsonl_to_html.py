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
    """Extract media URLs from text using regex patterns"""
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
    """Replace URLs in text with local paths"""
    if not text or not url_replacements:
        return text
    
    text = str(text)
    
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
    """Convert local media file paths in text to HTML img/video tags"""
    if not text:
        return text
    
    text = str(text)
    
    # Pattern to match local media paths like "downloaded_media/filename.jpg"
    # or just filenames that look like they're in downloaded_media
    media_pattern = rf'{re.escape(media_dir)}/[^\s<>")\]]+\.(jpg|jpeg|png|gif|webp|svg|bmp|ico|mp4|webm|avi|mov|wmv|flv|m4v|mpg|mpeg)'
    
    def replace_media_path(match):
        media_path = match.group(0)
        ext = os.path.splitext(media_path)[1].lower()
        
        # Check if file actually exists
        if not os.path.exists(media_path):
            return media_path  # Return as-is if file doesn't exist
        
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
    if 'replies' in data and data['replies']:
        if isinstance(data['replies'], dict) and data['replies'].get('kind') == 'Listing':
            for reply_item in data['replies']['data'].get('children', []):
                # Skip "more" comments
                if reply_item.get('kind') == 'more':
                    continue
                reply = parse_reddit_comment(reply_item, url_replacements, media_dir)
                if reply:
                    replies.append(reply)
    
    return {
        'id': comment_id,
        'author': author,
        'body': body,
        'score': score,
        'createdAt': created_utc,
        'replies': replies
    }


def merge_comment_replies(replies1, replies2):
    """Merge two lists of replies, matching by comment ID"""
    # Create a dict from replies1 for easy lookup
    merged = {reply['id']: reply.copy() for reply in replies1}
    
    # Merge replies2 into merged
    for reply2 in replies2:
        reply_id = reply2['id']
        if reply_id in merged:
            # Comment exists in both - merge their replies recursively
            merged_replies = merge_comment_replies(
                merged[reply_id].get('replies', []),
                reply2.get('replies', [])
            )
            # Keep the version with more content (prefer non-empty body)
            if not merged[reply_id].get('body') or merged[reply_id].get('body') == '[unavailable]':
                if reply2.get('body') and reply2.get('body') != '[unavailable]':
                    merged[reply_id] = reply2.copy()
            merged[reply_id]['replies'] = merged_replies
        else:
            # New comment, add it
            merged[reply_id] = reply2.copy()
    
    # Return as list, sorted by score (highest first) or creation time
    return sorted(merged.values(), key=lambda x: (x.get('score', 0), x.get('createdAt', 0)), reverse=True)


def merge_comment_trees(tree1, tree2):
    """Merge two comment trees, matching comments by ID and merging their replies"""
    merged = {k: v.copy() for k, v in tree1.items()}
    
    for comment_id, comment2 in tree2.items():
        if comment_id in merged:
            # Comment exists in both trees - merge their replies
            comment1 = merged[comment_id]
            merged_replies = merge_comment_replies(
                comment1.get('replies', []),
                comment2.get('replies', [])
            )
            # Keep the version with more content (prefer non-empty body)
            if not comment1.get('body') or comment1.get('body') == '[unavailable]':
                if comment2.get('body') and comment2.get('body') != '[unavailable]':
                    merged[comment_id] = comment2.copy()
            merged[comment_id]['replies'] = merged_replies
        else:
            # New comment, add it
            merged[comment_id] = comment2.copy()
    
    return merged


def parse_reddit_post(post_item, comments_listing, url_replacements=None, media_dir='downloaded_media'):
    """Parse a Reddit post and its associated comments"""
    if post_item.get('kind') != 't3':
        return None
    
    data = post_item.get('data', {})
    post_id = data.get('name', '')
    title = data.get('title', 'Untitled')
    selftext = data.get('selftext', '')
    author = data.get('author', '[deleted]')
    score = data.get('score', 0)
    created_utc = data.get('created_utc', 0)
    subreddit = data.get('subreddit', '')
    
    # Replace URLs in body text
    if url_replacements:
        selftext = replace_urls_in_text(selftext, url_replacements)
    
    # Embed media (convert local paths to HTML img/video tags)
    selftext = embed_media_in_text(selftext, media_dir)
    
    # Build comment tree from comments listing
    comment_tree = {}
    if comments_listing and comments_listing.get('kind') == 'Listing':
        for comment_item in comments_listing['data'].get('children', []):
            # Skip "more" comments
            if comment_item.get('kind') == 'more':
                continue
            comment = parse_reddit_comment(comment_item, url_replacements, media_dir)
            if comment:
                comment_tree[comment['id']] = comment
    
    return {
        'id': post_id,
        'title': title,
        'body': selftext,
        'author': author,
        'score': score,
        'createdAt': created_utc,
        'subreddit': subreddit,
        'comment_tree': comment_tree
    }


def format_timestamp(timestamp):
    """Format Unix timestamp to relative time"""
    if not timestamp:
        return ""
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        post_time = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        diff = now - post_time
        
        if diff.days > 365:
            years = diff.days // 365
            return f"{years}y ago"
        elif diff.days > 0:
            return f"{diff.days}d ago"
        elif diff.seconds > 3600:
            hours = diff.seconds // 3600
            return f"{hours}h ago"
        elif diff.seconds > 60:
            minutes = diff.seconds // 60
            return f"{minutes}m ago"
        else:
            return "just now"
    except:
        return ""


def comment_to_html(comment, depth=0):
    """Convert comment dictionary to HTML in Reddit style"""
    author = comment.get('author', '[deleted]')
    score = comment.get('score', 0)
    body = comment.get('body', '')
    created_at = comment.get('createdAt', 0)
    comment_id = comment.get('id', '')
    
    # Format author
    if author == '[deleted]':
        author_html = '<span class="deleted-author">[deleted]</span>'
    else:
        author_html = f'<a href="#" class="comment-author">u/{html_lib.escape(author)}</a>'
    
    # Format score with color
    score_class = "positive" if score > 0 else "negative" if score < 0 else ""
    score_display = f"{score:+d}" if score != 0 else "0"
    
    # Format timestamp
    time_str = format_timestamp(created_at)
    
    # If body contains HTML tags (from embedded media), use as-is, otherwise escape
    if '<img' in body or '<video' in body:
        body_html = body
    else:
        body_html = html_lib.escape(body)
    
    # Build comment HTML
    if depth == 0:
        html_str = f'''<div class="comment">
            <div class="comment-header">
                {author_html}
                <span class="comment-score {score_class}">{score_display} points</span>
                <span class="comment-time">{time_str}</span>
            </div>
            <div class="comment-body">{body_html}</div>'''
    else:
        html_str = f'''<div class="comment-thread">
            <div class="comment">
                <div class="comment-header">
                    {author_html}
                    <span class="comment-score {score_class}">{score_display} points</span>
                    <span class="comment-time">{time_str}</span>
                </div>
                <div class="comment-body">{body_html}</div>'''
    
    # Process replies recursively
    for reply in comment.get('replies', []):
        html_str += comment_to_html(reply, depth + 1)
    
    if depth == 0:
        html_str += "</div>"
    else:
        html_str += "</div></div>"
    
    return html_str


def generate_html(posts, url_to_local_path, output_file='media_aware_visualization.html'):
    """Generate HTML visualization from posts dictionary"""
    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>Reddit Conversation Visualizer</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * {{
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            margin: 0;
            padding: 0;
            background: #dae0e6;
            color: #1c1c1c;
            line-height: 1.5;
        }}
        .header {{
            background: #ffffff;
            border-bottom: 1px solid #edeff1;
            padding: 12px 16px;
            position: sticky;
            top: 0;
            z-index: 100;
            box-shadow: 0 2px 4px rgba(0,0,0,0.05);
        }}
        .header-content {{
            max-width: 1200px;
            margin: 0 auto;
            display: flex;
            align-items: center;
            gap: 16px;
        }}
        .header h1 {{
            margin: 0;
            font-size: 20px;
            font-weight: 700;
            color: #1a1a1b;
        }}
        .header-stats {{
            color: #7c7c7c;
            font-size: 14px;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            padding: 16px;
        }}
        .post {{
            background: #ffffff;
            border: 1px solid #ccc;
            border-radius: 4px;
            margin-bottom: 16px;
            overflow: hidden;
        }}
        .post-header {{
            padding: 12px 16px;
            border-bottom: 1px solid #edeff1;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}
        .post-header-left {{
            flex: 1;
        }}
        .post-title {{
            font-weight: 600;
            font-size: 18px;
            color: #1a1a1b;
            margin: 0 0 4px 0;
            line-height: 1.3;
        }}
        .post-meta {{
            font-size: 12px;
            color: #7c7c7c;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .subreddit {{
            font-weight: 600;
            color: #1a1a1b;
        }}
        .post-author {{
            color: #1a1a1b;
        }}
        .post-score {{
            color: #7c7c7c;
        }}
        .post-body {{
            padding: 16px;
            display: block;
            background: #ffffff;
            border-top: 1px solid #edeff1;
            color: #1c1c1c;
            white-space: pre-wrap;
            word-wrap: break-word;
        }}
        .comments-section {{
            padding: 0;
            display: block;
            background: #ffffff;
            border-top: 1px solid #edeff1;
        }}
        .comment {{
            padding: 8px 16px;
            border-left: 2px solid transparent;
            position: relative;
        }}
        .comment:hover {{
            background: #f8f9fa;
        }}
        .comment-thread {{
            border-left: 2px solid #edeff1;
            margin-left: 16px;
            padding-left: 8px;
        }}
        .comment-header {{
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 4px;
            font-size: 12px;
        }}
        .comment-author {{
            font-weight: 600;
            color: #1a1a1b;
            text-decoration: none;
        }}
        .comment-author:hover {{
            text-decoration: underline;
        }}
        .comment-score {{
            color: #7c7c7c;
            font-weight: 600;
        }}
        .comment-score.positive {{
            color: #ff4500;
        }}
        .comment-score.negative {{
            color: #7193ff;
        }}
        .comment-time {{
            color: #7c7c7c;
        }}
        .comment-body {{
            color: #1c1c1c;
            margin-top: 4px;
            word-wrap: break-word;
            line-height: 1.5;
        }}
        .comment-body img {{
            max-width: 100%;
            height: auto;
            margin: 8px 0;
            border-radius: 4px;
        }}
        .comment-body video {{
            max-width: 100%;
            height: auto;
            margin: 8px 0;
            border-radius: 4px;
        }}
        .deleted-author {{
            color: #7c7c7c;
            font-style: italic;
        }}
        .expand-btn {{
            background: none;
            border: none;
            color: #0079d3;
            cursor: pointer;
            font-size: 12px;
            padding: 4px 8px;
            margin-left: -8px;
        }}
        .expand-btn:hover {{
            text-decoration: underline;
        }}
    </style>
</head>
<body>
    <div class="header">
        <div class="header-content">
            <h1>📱 Reddit Conversation Visualizer</h1>
            <div class="header-stats">
                {len(posts):,} posts • {len(url_to_local_path):,} media files
            </div>
        </div>
    </div>
    <div class="container">
        <div class="post-list">

"""
    
    for post_id, post in posts.items():
        title = html_lib.escape(post.get('title', 'Untitled'))
        body = post.get('body', '')  # Already processed with URL replacements and media embedding
        comment_tree = post.get('comment_tree', {})
        author = html_lib.escape(post.get('author', '[deleted]'))
        score = post.get('score', 0)
        subreddit = html_lib.escape(post.get('subreddit', ''))
        created_at = post.get('createdAt', 0)
        time_str = format_timestamp(created_at)
        
        # If body contains HTML tags (from embedded media), use as-is, otherwise escape
        if '<img' in body or '<video' in body:
            body_html = body
        else:
            body_html = html_lib.escape(body)
        
        # Count comments
        def count_all_comments(tree):
            count = len(tree)
            for comment in tree.values():
                if comment.get('replies'):
                    count += len(comment['replies'])
            return count
        
        comment_count = count_all_comments(comment_tree)
        
        html_content += f"""            <div class="post">
                <div class="post-header">
                    <div class="post-header-left">
                        <div class="post-title">{title}</div>
                        <div class="post-meta">
                            <span class="subreddit">r/{subreddit}</span>
                            <span>•</span>
                            <span class="post-author">u/{author}</span>
                            <span>•</span>
                            <span class="post-score">{score} points</span>
                            <span>•</span>
                            <span class="comment-time">{time_str}</span>
                            <span>•</span>
                            <span>{comment_count} comments</span>
                        </div>
                    </div>
                </div>
                <div class="post-body" id="post-{post_id}">
                    {body_html}
                </div>
                <div class="comments-section" id="comments-{post_id}">
"""
        for comment_id, comment in comment_tree.items():
            html_content += comment_to_html(comment, 0)
        
        html_content += "</div></div>"
    
    html_content += """
        </div>
    </div>
</body>
</html>
"""
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    print(f"✓ HTML visualization saved to {output_file}")


def clean_text_for_training(text, preserve_media_paths=True, output_dir=None):
    """Remove HTML tags from text, optionally preserving local media paths for training"""
    if not text:
        return ""
    
    import re
    # Extract image paths from img tags and replace with just the path
    def replace_img_tag(match):
        img_tag = match.group(0)
        # Extract src attribute
        src_match = re.search(r'src=["\']([^"\']+)["\']', img_tag)
        if src_match:
            path = src_match.group(1)
            # Normalize path relative to output directory if provided
            if output_dir and preserve_media_paths:
                try:
                    # If path is absolute or relative, make it relative to output_dir
                    path_obj = Path(path)
                    if path_obj.is_absolute():
                        path = os.path.relpath(path, output_dir)
                    # If it's already relative, keep it as-is (should be relative to output_dir already)
                except:
                    pass  # Keep original path if normalization fails
            return path
        return '[image]'
    
    # Extract video paths from video tags
    def replace_video_tag(match):
        video_tag = match.group(0)
        # Extract src from source tag
        src_match = re.search(r'<source[^>]+src=["\']([^"\']+)["\']', video_tag)
        if src_match:
            path = src_match.group(1)
            # Normalize path relative to output directory if provided
            if output_dir and preserve_media_paths:
                try:
                    path_obj = Path(path)
                    if path_obj.is_absolute():
                        path = os.path.relpath(path, output_dir)
                except:
                    pass
            return path
        return '[video]'
    
    # Replace img tags with their src paths
    text = re.sub(r'<img[^>]*>', replace_img_tag, text)
    # Replace video tags with their src paths
    text = re.sub(r'<video[^>]*>.*?</video>', replace_video_tag, text, flags=re.DOTALL)
    # Remove any remaining HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Decode HTML entities
    text = html_lib.unescape(text)
    
    # If preserve_media_paths is False, replace paths with placeholders
    if not preserve_media_paths:
        # Replace local file paths with placeholders
        text = re.sub(r'[^\s<>")\]]+\.(jpg|jpeg|png|gif|webp|svg|bmp|ico|mp4|webm|avi|mov|wmv|flv|m4v|mpg|mpeg)', '[media]', text, flags=re.IGNORECASE)
    
    return text.strip()


def comment_to_dict(comment, preserve_media_paths=True, output_dir=None):
    """Convert comment object to dictionary for JSONL export"""
    return {
        'id': comment.get('id', ''),
        'author': comment.get('author', '[deleted]'),
        'body': clean_text_for_training(comment.get('body', ''), preserve_media_paths=preserve_media_paths, output_dir=output_dir),
        'score': comment.get('score', 0),
        'created_at': comment.get('createdAt', 0),
        'replies': [comment_to_dict(reply, preserve_media_paths=preserve_media_paths, output_dir=output_dir) for reply in comment.get('replies', [])]
    }


def export_to_jsonl(posts, output_file='conversation_data_cleaned.jsonl', preserve_media_paths=True, output_dir=None):
    """Export posts to cleaned JSONL format suitable for training"""
    print(f"\nExporting cleaned JSONL: {output_file}")
    if preserve_media_paths:
        print("  (Preserving local media file paths for training)")
    
    # Get output directory for path normalization
    if output_dir is None:
        output_dir = os.path.dirname(output_file) or '.'
    output_dir = Path(output_dir).resolve()
    
    try:
        exported_count = 0
        
        with open(output_file, 'w', encoding='utf-8') as f:
            for post_id, post in posts.items():
                try:
                    comment_tree = post.get('comment_tree', {})
                    
                    # Convert comment tree to list
                    comments_list = []
                    for comment_id, comment in comment_tree.items():
                        try:
                            comments_list.append(comment_to_dict(comment, preserve_media_paths=preserve_media_paths, output_dir=output_dir))
                        except Exception as e:
                            print(f"  Warning: Failed to process comment {comment_id}: {e}")
                            continue
                    
                    post_data = {
                        'id': post.get('id', ''),
                        'title': clean_text_for_training(post.get('title', ''), preserve_media_paths=preserve_media_paths),
                        'author': post.get('author', '[deleted]'),
                        'body': clean_text_for_training(post.get('body', ''), preserve_media_paths=preserve_media_paths, output_dir=output_dir),
                        'score': post.get('score', 0),
                        'created_at': post.get('createdAt', 0),
                        'subreddit': post.get('subreddit', ''),
                        'comment_count': len(comments_list),
                        'comments': comments_list
                    }
                    
                    f.write(json.dumps(post_data, ensure_ascii=False) + '\n')
                    exported_count += 1
                    
                except Exception as e:
                    print(f"  Warning: Failed to export post {post_id}: {e}")
                    continue
        
        print(f"✓ JSONL export saved to {output_file}")
        print(f"  - {exported_count} posts exported")
        
    except Exception as e:
        print(f"✗ JSONL export failed: {e}")
        import traceback
        traceback.print_exc()


def load_jsonl_file(input_file, posts_data, all_media_urls):
    """Load a single JSONL file and extract posts/comments"""
    print(f"Reading Reddit JSONL data from {input_file}...")
    
    with open(input_file, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            try:
                data = json.loads(line.strip())
                
                # Reddit API format: [Listing (posts), Listing (comments)]
                if not isinstance(data, list) or len(data) < 1:
                    print(f"  Warning: Line {line_num} is not in expected Reddit API format")
                    continue
                
                # First element should be posts listing
                posts_listing = data[0] if len(data) > 0 else None
                # Second element should be comments listing (if present)
                comments_listing = data[1] if len(data) > 1 else None
                
                if not posts_listing or posts_listing.get('kind') != 'Listing':
                    print(f"  Warning: Line {line_num} does not contain a posts listing")
                    continue
                
                # Process each post in the listing
                for post_item in posts_listing['data'].get('children', []):
                    if post_item.get('kind') != 't3':
                        continue
                    
                    post_data = post_item.get('data', {})
                    
                    # Extract media URLs from post
                    post_media = extract_media_from_reddit_post(post_data)
                    all_media_urls.update(post_media)
                    
                    # Extract media from comments
                    if comments_listing and comments_listing.get('kind') == 'Listing':
                        for comment_item in comments_listing['data'].get('children', []):
                            if comment_item.get('kind') == 't1':
                                comment_data = comment_item.get('data', {})
                                comment_media = extract_media_from_comment(comment_data)
                                all_media_urls.update(comment_media)
                                
                                # Also check replies recursively
                                if 'replies' in comment_data and comment_data['replies']:
                                    if isinstance(comment_data['replies'], dict) and comment_data['replies'].get('kind') == 'Listing':
                                        def extract_from_replies(replies_listing):
                                            for reply_item in replies_listing['data'].get('children', []):
                                                if reply_item.get('kind') == 't1':
                                                    reply_data = reply_item.get('data', {})
                                                    reply_media = extract_media_from_comment(reply_data)
                                                    all_media_urls.update(reply_media)
                                                    if 'replies' in reply_data and reply_data['replies']:
                                                        if isinstance(reply_data['replies'], dict) and reply_data['replies'].get('kind') == 'Listing':
                                                            extract_from_replies(reply_data['replies'])
                                        extract_from_replies(comment_data['replies'])
                    
                    # Store post data for later processing
                    post_id = post_data.get('name', '')
                    if post_id:
                        # If post already exists, we'll merge comments later
                        if post_id not in posts_data:
                            posts_data[post_id] = {
                                'post_item': post_item,
                                'comments_listings': []
                            }
                        # Add this comments listing to the list (we'll merge them later)
                        if comments_listing:
                            posts_data[post_id]['comments_listings'].append(comments_listing)
                        # Keep the most complete post_item (prefer one with more data)
                        existing_item = posts_data[post_id]['post_item']
                        if len(str(post_data.get('selftext', ''))) > len(str(existing_item.get('data', {}).get('selftext', ''))):
                            posts_data[post_id]['post_item'] = post_item
                        
            except json.JSONDecodeError as e:
                print(f"  Warning: Line {line_num} contains invalid JSON: {e}")
                continue
            except Exception as e:
                print(f"  Warning: Line {line_num} caused an error: {e}")
                continue
    
    print(f"✓ Processed posts from {input_file}")


def get_input_files(input_paths):
    """Get list of JSONL files from input paths (files or directories)"""
    input_files = []
    
    for path in input_paths:
        path_obj = Path(path)
        if path_obj.is_file():
            if path.endswith('.jsonl'):
                input_files.append(str(path_obj))
            else:
                print(f"  Warning: {path} is not a .jsonl file, skipping...")
        elif path_obj.is_dir():
            # Find all JSONL files in directory
            jsonl_files = list(path_obj.glob('*.jsonl'))
            if jsonl_files:
                input_files.extend([str(f) for f in jsonl_files])
                print(f"  Found {len(jsonl_files)} JSONL files in {path}")
            else:
                print(f"  Warning: No .jsonl files found in {path}")
        else:
            print(f"  Warning: {path} does not exist, skipping...")
    
    return input_files


def main():
    """Main function to convert JSONL to HTML"""
    parser = argparse.ArgumentParser(
        description='Convert Reddit JSONL files to HTML visualization with media downloads',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Process specific files
  python3 jsonl_to_html.py -i test.jsonl test3.jsonl -o output/
  
  # Process all JSONL files in a directory
  python3 jsonl_to_html.py -i data/ -o output/
  
  # Process files and directory together
  python3 jsonl_to_html.py -i file1.jsonl data/ -o output/
        """
    )
    
    parser.add_argument(
        '-i', '--input',
        nargs='+',
        required=True,
        help='Input JSONL file(s) or directory/directories containing JSONL files'
    )
    
    parser.add_argument(
        '-o', '--output',
        default='.',
        help='Output directory (default: current directory)'
    )
    
    parser.add_argument(
        '--html-name',
        default='media_aware_visualization.html',
        help='Output HTML filename (default: media_aware_visualization.html)'
    )
    
    parser.add_argument(
        '--jsonl-name',
        default='conversation_data_cleaned.jsonl',
        help='Output JSONL filename (default: conversation_data_cleaned.jsonl)'
    )
    
    parser.add_argument(
        '--media-dir',
        default='downloaded_media',
        help='Directory for downloaded media (default: downloaded_media)'
    )
    
    parser.add_argument(
        '--workers',
        type=int,
        default=50,
        help='Number of parallel workers for media downloads (default: 50)'
    )
    
    args = parser.parse_args()
    
    # Get input files
    input_files = get_input_files(args.input)
    
    if not input_files:
        print("Error: No valid JSONL files found!")
        return
    
    print(f"Processing {len(input_files)} JSONL file(s):")
    for f in input_files:
        print(f"  - {f}")
    
    # Setup output directory
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Setup paths
    output_file = output_dir / args.html_name
    jsonl_output = output_dir / args.jsonl_name
    media_dir = output_dir / args.media_dir
    
    # Create media directory
    media_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nMedia will be saved to: {media_dir}/")
    print(f"Output HTML: {output_file}")
    print(f"Output JSONL: {jsonl_output}")
    
    posts_data = {}
    all_media_urls = set()
    
    # First pass: Load all JSONL files and collect posts/comments
    for input_file in input_files:
        load_jsonl_file(input_file, posts_data, all_media_urls)
    
    print(f"\n✓ Total unique posts across all files: {len(posts_data)}")
    print(f"✓ Found {len(all_media_urls)} unique media URLs")
    
    if not posts_data:
        print("No posts found! Exiting.")
        return
    
    # Download all media
    url_to_local_path = {}
    if all_media_urls:
        downloaded = download_media_batch(list(all_media_urls), str(media_dir), url_to_local_path, max_workers=args.workers)
        url_to_local_path.update(downloaded)
    
    # Create URL replacements map (relative paths for HTML)
    url_replacements = {}
    for url, local_path in url_to_local_path.items():
        if local_path:
            # Calculate relative path from HTML file to media file
            relative_path = os.path.relpath(local_path, output_dir)
            url_replacements[url] = relative_path
    
    # Second pass: Parse posts with URL replacements and merge comments
    print("\nProcessing posts with downloaded media and merging comments...")
    posts = {}
    
    for post_id, post_info in posts_data.items():
        # Parse post with first comments listing
        comments_listings = post_info.get('comments_listings', [])
        
        if not comments_listings:
            # No comments, just parse the post
            post = parse_reddit_post(post_info['post_item'], None, url_replacements, str(media_dir))
            if post:
                posts[post_id] = post
        else:
            # Parse post with first comments listing
            post = parse_reddit_post(post_info['post_item'], comments_listings[0], url_replacements, str(media_dir))
            
            if post:
                # Merge comments from all listings
                merged_comment_tree = post['comment_tree']
                for comments_listing in comments_listings[1:]:
                    # Parse comments from this listing
                    temp_post = parse_reddit_post(post_info['post_item'], comments_listing, url_replacements, str(media_dir))
                    if temp_post:
                        # Merge comment trees
                        merged_comment_tree = merge_comment_trees(merged_comment_tree, temp_post['comment_tree'])
                
                post['comment_tree'] = merged_comment_tree
                posts[post_id] = post
                
                # Count total comments for logging
                def count_comments(tree):
                    count = len(tree)
                    for comment in tree.values():
                        if comment.get('replies'):
                            count += sum(1 for _ in comment['replies'])
                    return count
                
                total_comments = count_comments(merged_comment_tree)
                print(f"  Post {post_id}: {total_comments} total comments (merged from {len(comments_listings)} sources)")
    
    print(f"\nGenerating HTML visualization: {output_file}")
    generate_html(posts, url_to_local_path, str(output_file))
    
    # Export cleaned JSONL for training (preserving local media paths)
    export_to_jsonl(posts, str(jsonl_output), preserve_media_paths=True, output_dir=output_dir)
    
    print("\n✅ Complete!")
    print(f"📄 Open {output_file} to view the output.")
    print(f"📁 Media files are in the {media_dir}/ directory.")
    print(f"📋 Cleaned JSONL for training: {jsonl_output}")


if __name__ == "__main__":
    main()
