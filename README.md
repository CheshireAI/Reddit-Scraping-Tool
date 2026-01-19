# Reddit JSONL to HTML Converter

A Python script that converts Reddit JSONL files (Reddit API format) into a beautiful HTML visualization with embedded media. The script downloads images and videos, merges comments from multiple sources, and exports cleaned data for training.

## Features

- **Media-Aware**: Automatically downloads images and videos from posts and comments
- **Giphy Support**: Extracts and downloads giphy links (e.g., `![gif](giphy|ID)`)
- **Preview Images**: Embeds preview and thumbnail images even if not in post body text
- **Comment Merging**: Combines comments from multiple JSONL files (useful when users block each other)
- **Reddit-Style UI**: Beautiful HTML output that looks like Reddit
- **Training Data Export**: Exports cleaned JSONL format with local media paths for training
- **Parallel Downloads**: Fast media downloads using multiple workers

## Installation

No special dependencies beyond standard Python libraries. The script uses:
- `json` - JSON parsing
- `html` - HTML escaping
- `requests` - Media downloads
- `argparse` - Command-line arguments
- `pathlib` - Path handling
- `concurrent.futures` - Parallel downloads

## Usage

### Basic Usage

Process specific JSONL files:
```bash
python3 jsonl_to_html.py -i test.jsonl test3.jsonl
```

Process all JSONL files in a directory:
```bash
python3 jsonl_to_html.py -i data/
```

Process files and directories together:
```bash
python3 jsonl_to_html.py -i file1.jsonl file2.jsonl data/ -o output/
```

### Command-Line Options

```
-i, --input        Input JSONL file(s) or directory/directories (required)
-o, --output       Output directory (default: current directory)
--html-name        Output HTML filename (default: media_aware_visualization.html)
--jsonl-name      Output JSONL filename (default: conversation_data_cleaned.jsonl)
--media-dir        Directory for downloaded media (default: downloaded_media)
--workers          Number of parallel workers for downloads (default: 50)
```

### Examples

```bash
# Process files and save to custom output directory
python3 jsonl_to_html.py -i test.jsonl test3.jsonl -o results/

# Process directory with custom filenames
python3 jsonl_to_html.py -i data/ -o output/ --html-name reddit_threads.html --jsonl-name training_data.jsonl

# Use more workers for faster downloads
python3 jsonl_to_html.py -i large_dataset/ -o output/ --workers 100
```

## Input Format

The script expects JSONL files in Reddit API format. Each line should be a JSON array with:
- `[0]`: Listing containing posts (kind="Listing", children with kind="t3")
- `[1]`: Listing containing comments (kind="Listing", children with kind="t1")

Example structure:
```json
[
  {
    "kind": "Listing",
    "data": {
      "children": [
        {
          "kind": "t3",
          "data": {
            "name": "t3_xxxxx",
            "title": "Post Title",
            "selftext": "Post body text",
            "author": "username",
            "score": 100,
            "subreddit": "subredditname",
            "created_utc": 1234567890,
            ...
          }
        }
      ]
    }
  },
  {
    "kind": "Listing",
    "data": {
      "children": [
        {
          "kind": "t1",
          "data": {
            "name": "t1_xxxxx",
            "body": "Comment text",
            "author": "username",
            "score": 50,
            "created_utc": 1234567890,
            "replies": { ... }
          }
        }
      ]
    }
  }
]
```

## Output Files

### HTML Visualization (`media_aware_visualization.html`)

A Reddit-style HTML page showing:
- Post titles, authors, scores, timestamps
- Post bodies with embedded images/videos
- Nested comment threads with proper indentation
- All media files embedded inline

### Cleaned JSONL (`conversation_data_cleaned.jsonl`)

Training-ready format with:
- One post per line
- Cleaned text (HTML tags removed, media paths preserved as local file references)
- Nested comment structure preserved
- All metadata (scores, timestamps, authors)
- Local media paths for training (e.g., `downloaded_media/filename.jpg`)

Example output line:
```json
{
  "id": "t3_xxxxx",
  "title": "Post Title",
  "author": "username",
  "body": "Post body text downloaded_media/image1.jpg",
  "score": 100,
  "created_at": 1234567890,
  "subreddit": "subredditname",
  "comment_count": 5,
  "comments": [
    {
      "id": "t1_xxxxx",
      "author": "commenter",
      "body": "Comment text downloaded_media/image2.jpg",
      "score": 50,
      "created_at": 1234567890,
      "replies": [...]
    }
  ]
}
```

### Media Directory (`downloaded_media/`)

All downloaded images and videos are saved here with MD5-based filenames to avoid duplicates.

## How It Works

### 1. Loading Phase
- Reads all specified JSONL files
- Extracts posts and comments from Reddit API format
- Collects all media URLs from posts and comments

### 2. Media Download Phase
- Downloads all unique media files in parallel
- Saves to `downloaded_media/` directory
- Creates mapping from original URLs to local paths

### 3. Processing Phase
- Merges comments from multiple files (by matching comment IDs)
- Replaces media URLs with local file paths
- Converts giphy links to local file paths
- Embeds all images/videos as HTML tags (including preview/thumbnail images)
- Processes nested comment threads recursively

### 4. Export Phase
- Generates Reddit-style HTML visualization
- Exports cleaned JSONL for training data

## Comment Merging

When multiple JSONL files contain the same post, the script:
- Matches comments by their Reddit ID
- Merges replies recursively
- Combines all unique comments from all sources
- Useful when users block each other and different files show different parts of the conversation

## Media Handling

- **Extraction**: Finds media URLs in:
  - Post `url` and `url_overridden_by_dest` fields
  - Post `preview` images (automatically embedded even if not in body)
  - Post `thumbnail` images (automatically embedded even if not in body)
  - Gallery images (`gallery_data`/`media_metadata`)
  - URLs in post/comment body text
  - Giphy links: `![gif](giphy|ID)` or `giphy|ID` format

- **Download**: 
  - Parallel downloads using ThreadPoolExecutor
  - Retry logic for failed downloads
  - Deduplication by URL
  - Progress tracking
  - Giphy GIFs are downloaded and saved locally

- **Embedding**:
  - All downloaded images are embedded as `<img>` tags in HTML
  - Videos embedded as `<video>` tags with controls
  - Preview/thumbnail images included even if not in post body text
  - Giphy links converted to local image references
  - Responsive styling for all media

## Troubleshooting

### No files found
- Check that input paths are correct
- Ensure JSONL files have `.jsonl` extension
- Verify file permissions

### Media downloads failing
- Check internet connection
- Some URLs may be expired or require authentication
- Failed downloads are logged but don't stop processing

### HTML not displaying images
- Ensure `downloaded_media/` directory is in the same location as HTML file
- Check that media files were actually downloaded
- Verify relative paths in HTML source
- All downloaded files should be embedded as `<img>` tags - check HTML source to confirm

### Giphy links not working
- Giphy links are automatically extracted and downloaded
- Format: `![gif](giphy|ID)` or just `giphy|ID` in text
- Downloaded giphy files are saved with MD5-based filenames
- If a giphy fails to download, it will fall back to external URL

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
