"""Structure-aware PDF extraction built on PyMuPDF.

PDF does not carry Word-style paragraph objects. This module reconstructs a
useful document-flow model from page geometry while keeping the metadata
needed later for preview, rewrite protection and DOCX export.
"""

from collections import Counter
import hashlib
import logging
import re
import statistics


_HEADING_NUMBER = re.compile(
    r"^(?:chapter\s+\d+\s*[:.\-]?\s*|"
    r"\d+(?:\.\d+)*\.?\s+|"
    r"[（(]\s*\d+\s*[）)]\s*)",
    re.IGNORECASE,
)
_SEMANTIC_HEADING = re.compile(
    r"^(?:abstract|summary|introduction|background|methods?|methodology|"
    r"results?|analysis|discussion|conclusions?|concluding remarks|"
    r"references?|bibliography|appendix|acknowledg(?:e)?ments?|"
    r"摘要|引言|绪论|背景|方法|结果|讨论|结论|参考文献|附录|致谢)$",
    re.IGNORECASE,
)
_CONTENT_HEADING = re.compile(
    r"^(?:abstract|introduction|background|chapter\s+1|摘要|引言|绪论|正文)$",
    re.IGNORECASE,
)
_TOC_HEADING = re.compile(
    r"^(?:table\s+of\s+contents|contents|list\s+of\s+(?:figures|tables)|"
    r"目录|图目录|表目录)$",
    re.IGNORECASE,
)
_CAPTION = re.compile(
    r"^(?:figure|fig\.?|table|chart|图|表)\s*[A-Z]?\d+(?:[.\-]\d+)*\s*[:.\-]?",
    re.IGNORECASE,
)
_LIST_MARKER = re.compile(
    r"^\s*((?:[•●◦▪‣⁃])|(?:[-–—])|(?:\(?\d+[.)）])|(?:[A-Za-z][.)]))\s+"
)


def _load_pymupdf():
    try:
        import pymupdf
        module = pymupdf
    except ImportError:  # PyMuPDF < 1.24 compatibility
        import fitz
        module = fitz
    if hasattr(module, 'no_recommend_layout'):
        module.no_recommend_layout()
    return module


def _join_text(left, right):
    left = left.rstrip()
    right = right.lstrip()
    if not left:
        return right
    if not right:
        return left
    if left.endswith('-') and not left.endswith(('<-', '--')):
        return left + right
    return left + ' ' + right


def _clean_block(block):
    lines = []
    for line in block.get('lines', []):
        text = ''.join(span.get('text', '') for span in line.get('spans', []))
        text = text.strip()
        if text:
            lines.append(text)
    if not lines:
        return ''
    merged = lines[0]
    for line in lines[1:]:
        merged = _join_text(merged, line)
    return merged


def _rect(value):
    return [round(float(number), 3) for number in value]


def _union_bbox(boxes):
    boxes = [box for box in boxes if box]
    if not boxes:
        return None
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def _intersection_area(left, right):
    width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    return width * height


def _overlap_ratio(left, right):
    area = max(0.0, (left[2] - left[0]) * (left[3] - left[1]))
    return _intersection_area(left, right) / area if area else 0.0


def _strip_heading_number(text):
    return _HEADING_NUMBER.sub('', (text or '').strip()).strip(' .:–—-')


def _normalize_heading(text):
    text = _strip_heading_number(text).casefold()
    return re.sub(r'[^\w]+', '', text, flags=re.UNICODE)


def _heading_number_level(text):
    match = re.match(r'^\s*(\d+(?:\.\d+)*)\.?\s+', text or '')
    if not match:
        return None
    return min(6, match.group(1).count('.') + 1)


def _block_font_stats(block):
    weights = Counter()
    font_weights = Counter()
    flag_weights = Counter()
    total = 0
    for line in block.get('lines', []):
        for span in line.get('spans', []):
            text = span.get('text', '')
            weight = max(1, len(text.strip()))
            size = round(float(span.get('size') or 0), 2)
            flags = int(span.get('flags') or 0)
            font = span.get('font') or ''
            weights[size] += weight
            font_weights[font] += weight
            flag_weights[flags] += weight
            total += weight
    size = weights.most_common(1)[0][0] if weights else 0.0
    font = font_weights.most_common(1)[0][0] if font_weights else None
    bold = sum(weight for flags, weight in flag_weights.items() if flags & 16)
    italic = sum(weight for flags, weight in flag_weights.items() if flags & 2)
    mono = sum(weight for flags, weight in flag_weights.items() if flags & 8)
    return {
        'font_size': size,
        'font_name': font,
        'is_bold': bool(total and bold / total >= 0.5),
        'is_italic': bool(total and italic / total >= 0.5),
        'is_monospace': bool(total and mono / total >= 0.6),
    }


def _image_hash(image_info):
    digest = image_info.get('digest')
    if isinstance(digest, bytes):
        return digest.hex()
    if digest:
        return str(digest)
    return hashlib.sha1(repr(image_info.get('bbox')).encode()).hexdigest()


def _first_pass(doc, fitz):
    """Read each page once and collect document-wide structure statistics."""
    font_weights = Counter()
    image_counts = Counter()
    page_records = []
    text_flags = fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES
    for page in doc:
        height = float(page.rect.height)
        top_limit = height * 0.07
        bottom_limit = height * 0.93
        page_dict = page.get_text('dict', sort=True, flags=text_flags)
        for block in page_dict.get('blocks', []):
            bbox = block.get('bbox') or (0, 0, 0, 0)
            if bbox[1] < top_limit or bbox[3] > bottom_limit:
                continue
            if block.get('type') == 0:
                for line in block.get('lines', []):
                    for span in line.get('spans', []):
                        text = span.get('text', '').strip()
                        if text:
                            size = round(float(span.get('size') or 0), 1)
                            font_weights[size] += len(text)
        try:
            images = page.get_image_info(hashes=True, xrefs=True)
        except Exception:
            images = []
        for image in images:
            image_counts[_image_hash(image)] += 1
        page_records.append({'page_dict': page_dict, 'images': images})
    body_size = font_weights.most_common(1)[0][0] if font_weights else 11.0
    return float(body_size), image_counts, page_records


def _outline_metadata(doc):
    by_page = {}
    first_content_page = None
    try:
        toc = doc.get_toc() or []
    except Exception:
        toc = []
    for entry in toc:
        if len(entry) < 3:
            continue
        level, title, page_number = entry[:3]
        page_index = max(0, int(page_number) - 1)
        item = {
            'level': max(1, min(6, int(level))),
            'title': str(title),
            'normalized': _normalize_heading(str(title)),
        }
        by_page.setdefault(page_index, []).append(item)
        if _CONTENT_HEADING.match(_strip_heading_number(str(title))):
            if first_content_page is None or page_index < first_content_page:
                first_content_page = page_index
    return by_page, first_content_page


def _match_outline(text, page_entries):
    normalized = _normalize_heading(text)
    if not normalized:
        return None
    exact = [item for item in page_entries if item['normalized'] == normalized]
    if exact:
        return exact[0]['level']
    if len(normalized) >= 8:
        for item in page_entries:
            target = item['normalized']
            size_ratio = min(len(target), len(normalized)) / max(len(target), len(normalized)) \
                if target else 0
            if (
                len(target) >= 8 and size_ratio >= 0.55 and
                (target.startswith(normalized) or normalized.startswith(target))
            ):
                return item['level']
    return None


def _link_rectangles(page):
    internal = []
    external = []
    try:
        links = page.get_links() or []
    except Exception:
        links = []
    for link in links:
        rect = link.get('from')
        if rect is None:
            continue
        target = internal if link.get('page', -1) >= 0 else external
        target.append(_rect(rect))
    return internal, external


def _line_items(block_index, line):
    """Split a visual line into cells when spans have a column-sized gap."""
    groups = []
    for span in line.get('spans', []):
        text = span.get('text', '')
        if not text.strip():
            continue
        bbox = _rect(span.get('bbox') or line.get('bbox') or (0, 0, 0, 0))
        if groups and bbox[0] - groups[-1]['bbox'][2] <= 8.0:
            groups[-1]['text'] = _join_text(groups[-1]['text'], text)
            groups[-1]['bbox'] = _union_bbox([groups[-1]['bbox'], bbox])
        else:
            groups.append({
                'text': text.strip(),
                'bbox': bbox,
                'block_indexes': {block_index},
            })
    return groups


def _merge_row_cells(items):
    cells = []
    for item in sorted(items, key=lambda value: value['bbox'][0]):
        if not cells:
            cells.append(dict(item))
            continue
        previous = cells[-1]
        gap = item['bbox'][0] - previous['bbox'][2]
        if gap <= 4.0:
            previous['text'] = _join_text(previous['text'], item['text'])
            previous['bbox'] = _union_bbox([previous['bbox'], item['bbox']])
            previous['block_indexes'] |= item['block_indexes']
        else:
            cells.append(dict(item))
    return cells


def _merge_cell_text(items):
    text = ''
    for item in sorted(items, key=lambda value: (
            value['bbox'][1], value['bbox'][0])):
        text = _join_text(text, item['text'])
    return text


def _rows_align(previous, following, page_width):
    if abs(len(previous['cells']) - len(following['cells'])) > 1:
        return False
    tolerance = max(12.0, page_width * 0.025)
    lefts = [cell['bbox'][0] for cell in previous['cells']]
    matches = sum(
        1 for cell in following['cells']
        if any(abs(cell['bbox'][0] - left) <= tolerance for left in lefts)
    )
    return matches >= min(2, len(previous['cells']), len(following['cells']))


def _valid_table_rows(rows, page_width):
    if len(rows) < 2:
        return False
    counts = Counter(len(row['cells']) for row in rows)
    columns, frequency = counts.most_common(1)[0]
    if columns < 2 or frequency < 2:
        return False
    if columns >= 3:
        return True
    if len(rows) < 3:
        return False
    if all(
        re.fullmatch(r'\d+(?:\.\d+)*\.?', row['cells'][0]['text'].strip())
        for row in rows if len(row['cells']) == 2
    ):
        # Adjacent numbered headings ("1 Introduction", "1.1 Context")
        # align like a two-column table but are document structure.
        return False
    values = [cell['text'] for row in rows for cell in row['cells']]
    average_words = sum(len(value.split()) for value in values) / max(1, len(values))
    numeric = sum(bool(re.search(r'\d', value)) for value in values) / max(1, len(values))
    average_width = sum(
        cell['bbox'][2] - cell['bbox'][0]
        for row in rows for cell in row['cells']
    ) / max(1, len(values))
    return numeric >= 0.15 or average_words <= 4 or average_width < page_width * 0.2


def _text_table_nodes(blocks, page_width, page_number, first_table_index):
    """Recover borderless tables from repeated, horizontally aligned rows."""
    line_items = []
    for block_index, block in enumerate(blocks):
        if block.get('type') != 0:
            continue
        for line in block.get('lines', []):
            line_items.extend(_line_items(block_index, line))

    groups = []
    for item in sorted(line_items, key=lambda value: (value['bbox'][1], value['bbox'][0])):
        center = (item['bbox'][1] + item['bbox'][3]) / 2
        if groups and abs(groups[-1]['center'] - center) <= 2.5:
            groups[-1]['items'].append(item)
            count = len(groups[-1]['items'])
            groups[-1]['center'] = (groups[-1]['center'] * (count - 1) + center) / count
        else:
            groups.append({'center': center, 'items': [item]})

    candidate_rows = []
    for group in groups:
        cells = _merge_row_cells(group['items'])
        if not 2 <= len(cells) <= 6:
            continue
        # Wrapped text in later columns is a continuation of the row above,
        # not a new logical row. Real table rows start near the left margin.
        if cells[0]['bbox'][0] > page_width * 0.25:
            continue
        if min(cells[index]['bbox'][0] - cells[index - 1]['bbox'][2]
               for index in range(1, len(cells))) < 6.0:
            continue
        candidate_rows.append({
            'cells': cells,
            'bbox': _union_bbox([cell['bbox'] for cell in cells]),
        })

    clusters = []
    current = []
    for row in candidate_rows:
        if current:
            gap = row['bbox'][1] - current[-1]['bbox'][3]
            if gap > 110 or not _rows_align(current[-1], row, page_width):
                clusters.append(current)
                current = []
        current.append(row)
    if current:
        clusters.append(current)

    nodes = []
    excluded = set()
    table_index = first_table_index
    for cluster in clusters:
        if not _valid_table_rows(cluster, page_width):
            continue
        column_count = Counter(len(row['cells']) for row in cluster).most_common(1)[0][0]
        rows = [row for row in cluster if len(row['cells']) == column_count]
        if len(rows) < 2:
            continue
        anchors = [cell['bbox'][0] for cell in rows[0]['cells']]
        column_starts = [
            (rows[0]['cells'][index - 1]['bbox'][2] + anchors[index]) / 2
            for index in range(1, len(anchors))
        ]
        row_steps = [
            rows[index + 1]['bbox'][1] - rows[index]['bbox'][1]
            for index in range(len(rows) - 1)
        ]
        typical_step = sorted(row_steps)[len(row_steps) // 2] if row_steps else 32.0
        typical_step = min(110.0, max(24.0, typical_step))
        values = []
        row_items = []
        for row_index, row in enumerate(rows):
            top = row['bbox'][1] - 1.0
            bottom = (
                rows[row_index + 1]['bbox'][1] - 1.0
                if row_index + 1 < len(rows)
                else top + typical_step
            )
            included = [
                item for item in line_items
                if top <= (item['bbox'][1] + item['bbox'][3]) / 2 < bottom
                and anchors[0] - 8.0 <= item['bbox'][0] <= page_width - 20.0
            ]
            cells = [[] for _ in range(column_count)]
            for item in sorted(included, key=lambda value: (
                    value['bbox'][1], value['bbox'][0])):
                column = sum(
                    item['bbox'][0] >= column_start
                    for column_start in column_starts
                )
                cells[min(column, column_count - 1)].append(item)
            row_items.extend(included)
            values.append([
                _merge_cell_text(items) for items in cells
            ])
        table_index += 1
        block_indexes = set().union(*(
            item['block_indexes'] for item in row_items
        )) if row_items else set()
        excluded |= block_indexes
        bbox = _union_bbox([item['bbox'] for item in row_items])
        nodes.append({
            'node_type': 'table',
            'table': table_index,
            'headers': values[0],
            'rows': values[1:],
            'row_count': len(values),
            'column_count': column_count,
            'page_number': page_number,
            'bbox': bbox,
            'source_bboxes': [bbox],
        })
    return nodes, excluded, table_index


def _ruled_table_nodes(page, occupied, page_number, first_table_index):
    """Use PyMuPDF's native finder for ruled tables missed by row alignment."""
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
    if len(drawings) < 4 or not hasattr(page, 'find_tables'):
        return [], first_table_index
    try:
        finder = page.find_tables(strategy='lines_strict')
    except Exception:
        logging.exception('PyMuPDF table detection failed on page %s', page_number)
        return [], first_table_index

    nodes = []
    table_index = first_table_index
    for table in getattr(finder, 'tables', []):
        bbox = _rect(table.bbox)
        if any(_overlap_ratio(bbox, other) >= 0.5 for other in occupied):
            continue
        values = table.extract() or []
        values = [
            [('' if cell is None else str(cell).strip()) for cell in row]
            for row in values if row
        ]
        if len(values) < 2 or max((len(row) for row in values), default=0) < 2:
            continue
        columns = max(len(row) for row in values)
        values = [row + [''] * (columns - len(row)) for row in values]
        table_index += 1
        nodes.append({
            'node_type': 'table',
            'table': table_index,
            'headers': values[0],
            'rows': values[1:],
            'row_count': len(values),
            'column_count': columns,
            'page_number': page_number,
            'bbox': bbox,
            'source_bboxes': [bbox],
        })
    return nodes, table_index


def _heading_metadata(text, font, body_size, page_index, page_entries, is_toc):
    words = text.split()
    short = len(words) <= 25 and len(text) <= 180
    visual_short = len(words) <= 12 and not re.search(r'[.!?。！？][\]\)"\']?$', text)
    outline_level = _match_outline(text, page_entries)
    number_level = _heading_number_level(text)
    semantic = bool(_SEMANTIC_HEADING.match(_strip_heading_number(text)))
    ratio = (font['font_size'] / body_size) if body_size else 1.0

    level = outline_level
    if level is None and short and number_level is not None and (
            ratio >= 1.04 or font['is_bold']):
        level = number_level
    if level is None and short and semantic:
        level = 1
    if level is None and visual_short and ratio >= 1.35:
        level = 1
    if level is None and visual_short and ratio >= 1.16:
        level = 2
    if level is None and visual_short and font['is_bold'] and ratio >= 1.04:
        level = 3
    if level is None and is_toc and _TOC_HEADING.match(text.strip()):
        level = 1

    title = bool(level == 1 and page_index == 0 and ratio >= 1.45)
    if level is None:
        return None, None
    if title:
        return 0, 'Title'
    level = max(1, min(6, int(level)))
    return level, f'Heading {level}'


def _text_node(block, page_index, body_size, page_entries,
               first_content_page, internal_links, external_links):
    text = _clean_block(block)
    if not text:
        return None
    bbox = _rect(block.get('bbox') or (0, 0, 0, 0))
    font = _block_font_stats(block)
    internal_overlaps = sum(_intersection_area(bbox, rect) > 0 for rect in internal_links)
    external_overlaps = sum(_intersection_area(bbox, rect) > 0 for rect in external_links)
    is_toc = bool(
        _TOC_HEADING.match(text.strip()) or
        (len(internal_links) >= 3 and internal_overlaps >= 1) or
        len(re.findall(r'(?:\.\s*){4,}', text)) >= 2
    )
    heading_level, style = _heading_metadata(
        text, font, body_size, page_index, page_entries, is_toc
    )
    caption = bool(_CAPTION.match(text) and len(text.split()) <= 30)
    list_match = _LIST_MARKER.match(text)
    node = {
        'node_type': 'heading' if heading_level is not None else (
            'caption' if caption else ('code' if font['is_monospace'] else 'paragraph')
        ),
        'text': text,
        'word_count': len(text.split()),
        'page_number': page_index + 1,
        'bbox': bbox,
        'source_bboxes': [bbox],
        'font_size': font['font_size'],
        'font_name': font['font_name'],
        'is_bold': font['is_bold'],
        'is_italic': font['is_italic'],
        'is_heading': heading_level is not None,
        'heading_level': heading_level,
        'style': style or ('Caption' if caption else None),
        'is_toc': is_toc,
        'is_front_matter': bool(
            first_content_page is not None and page_index < first_content_page
        ),
        'is_caption': caption,
        'is_code_block': font['is_monospace'],
        'has_hyperlink': external_overlaps > 0,
    }
    if list_match:
        node['list_text'] = list_match.group(1)
        node['list_level'] = 0
    return node


def _image_node(image_info, page_index, page_rect, repeat_counts, image_index):
    bbox = _rect(image_info.get('bbox') or (0, 0, 0, 0))
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    page_width = float(page_rect.width)
    page_height = float(page_rect.height)
    fingerprint = _image_hash(image_info)
    repeated = bool(fingerprint and repeat_counts.get(fingerprint, 0) >= 3)
    near_margin = bbox[1] < page_height * 0.07 or bbox[3] > page_height * 0.93
    too_small = (
        width < 36 or height < 24 or
        width * height < page_width * page_height * 0.003
    )
    if repeated or near_margin or too_small:
        return None
    return {
        'node_type': 'image',
        'image': image_index,
        'page_number': page_index + 1,
        'bbox': bbox,
        'source_bboxes': [bbox],
        'image_hash': fingerprint,
        'image_ext': 'png',
        'image_xref': int(image_info.get('xref') or 0),
        'image_width': int(image_info.get('width') or 0),
        'image_height': int(image_info.get('height') or 0),
        'display_width': round(width, 3),
        'display_height': round(height, 3),
    }


def _can_merge_text(previous, following, paragraph_gap):
    if previous.get('node_type') != 'paragraph' or following.get('node_type') != 'paragraph':
        return False
    protected = ('is_toc', 'is_caption', 'is_code_block', 'has_hyperlink')
    if any(previous.get(key) or following.get(key) for key in protected):
        return False
    if previous.get('list_text') or following.get('list_text'):
        return False
    left = previous['bbox']
    right = following['bbox']
    gap = max(0.0, right[1] - left[3])
    horizontal_overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    minimum_width = max(1.0, min(left[2] - left[0], right[2] - right[0]))
    same_column = horizontal_overlap / minimum_width >= 0.45 or abs(left[0] - right[0]) <= 24
    similar_size = abs((previous.get('font_size') or 0) - (following.get('font_size') or 0)) <= 1.0
    return gap <= paragraph_gap and same_column and similar_size


def _merge_nodes(previous, following):
    previous['text'] = _join_text(previous['text'], following['text'])
    previous['word_count'] = len(previous['text'].split())
    previous['bbox'] = _union_bbox([previous['bbox'], following['bbox']])
    previous.setdefault('source_bboxes', []).extend(following.get('source_bboxes', []))
    previous['page_end'] = following.get('page_end', following.get('page_number'))
    return previous


def _merge_page_nodes(nodes):
    text_nodes = [node for node in nodes if node.get('text')]
    gaps = []
    for index in range(1, len(text_nodes)):
        if text_nodes[index - 1].get('page_number') != text_nodes[index].get('page_number'):
            continue
        gap = text_nodes[index]['bbox'][1] - text_nodes[index - 1]['bbox'][3]
        if gap > 0.5:
            gaps.append(gap)
    normal_gap = statistics.median(gaps) if gaps else 6.0
    paragraph_gap = max(8.0, min(14.0, normal_gap * 1.6))

    merged = []
    for node in nodes:
        if merged and _can_merge_text(merged[-1], node, paragraph_gap):
            _merge_nodes(merged[-1], node)
            continue
        if (
            merged and merged[-1].get('node_type') == 'heading' and
            node.get('node_type') == 'heading' and
            merged[-1].get('heading_level') == node.get('heading_level') and
            node['bbox'][1] - merged[-1]['bbox'][3] <= 10
        ):
            _merge_nodes(merged[-1], node)
            continue
        merged.append(node)
    return merged


def _continues(previous, following):
    if previous.get('node_type') != 'paragraph' or following.get('node_type') != 'paragraph':
        return False
    if any(previous.get(key) or following.get(key)
           for key in ('is_toc', 'is_front_matter', 'is_reference', 'list_text')):
        return False
    left = (previous.get('text') or '').rstrip()
    right = (following.get('text') or '').lstrip()
    if not left or not right:
        return False
    if re.match(r'^\d+(?:\.\d+)*\.?\s+[A-Z]', right):
        return False
    return left.endswith(('-', ',', ';', ':')) or not re.search(
        r'[.!?][\]\)"\']?$', left
    )


def extract_text_from_pdf(filepath):
    """Extract ordered headings, paragraphs, tables and image references."""
    fitz = _load_pymupdf()
    doc = fitz.open(filepath)
    try:
        body_size, repeat_counts, page_records = _first_pass(doc, fitz)
        outline_by_page, first_content_page = _outline_metadata(doc)
        first_two_pages_text = ''.join(
            doc[index].get_text() for index in range(min(2, len(doc)))
        )
        is_turnitin = 'turnitin' in first_two_pages_text.lower()
        start_page = 2 if is_turnitin else 0
        nodes = []
        table_index = 0
        image_index = 0

        for page_index in range(start_page, len(doc)):
            page = doc[page_index]
            page_height = float(page.rect.height)
            top_limit = page_height * 0.07
            bottom_limit = page_height * 0.93
            page_record = page_records[page_index]
            page_dict = page_record['page_dict']
            blocks = page_dict.get('blocks', [])
            internal_links, external_links = _link_rectangles(page)

            tables, excluded, table_index = _text_table_nodes(
                blocks, float(page.rect.width), page_index + 1, table_index
            )
            if tables:
                ruled_tables = []
            else:
                ruled_tables, table_index = _ruled_table_nodes(
                    page, [], page_index + 1, table_index,
                )
            tables.extend(ruled_tables)
            table_boxes = [item['bbox'] for item in tables]

            page_nodes = list(tables)
            for block_index, block in enumerate(blocks):
                bbox = _rect(block.get('bbox') or (0, 0, 0, 0))
                if bbox[1] < top_limit or bbox[3] > bottom_limit:
                    continue
                if block_index in excluded:
                    continue
                if any(_overlap_ratio(bbox, table_box) >= 0.55 for table_box in table_boxes):
                    continue
                if block.get('type') == 0:
                    node = _text_node(
                        block, page_index, body_size,
                        outline_by_page.get(page_index, []), first_content_page,
                        internal_links, external_links,
                    )
                else:
                    node = None
                if node:
                    page_nodes.append(node)

            for image_info in page_record['images']:
                image_index += 1
                node = _image_node(
                    image_info, page_index, page.rect, repeat_counts, image_index
                )
                if node:
                    page_nodes.append(node)
                else:
                    image_index -= 1

            page_nodes.sort(key=lambda item: (
                item.get('bbox', [0, 0, 0, 0])[1],
                item.get('bbox', [0, 0, 0, 0])[0],
                0 if item.get('node_type') == 'heading' else 1,
            ))
            page_nodes = _merge_page_nodes(page_nodes)
            if nodes and page_nodes and _continues(nodes[-1], page_nodes[0]):
                _merge_nodes(nodes[-1], page_nodes.pop(0))
            nodes.extend(page_nodes)

        if is_turnitin:
            logging.info(
                'Turnitin report detected, skipped first 2 pages (%s pages total)',
                len(doc),
            )
        logging.info(
            'PDF structure extracted pages=%s nodes=%s headings=%s tables=%s images=%s body_font=%.1f',
            len(doc), len(nodes),
            sum(bool(node.get('is_heading')) for node in nodes),
            sum(node.get('node_type') == 'table' for node in nodes),
            sum(node.get('node_type') == 'image' for node in nodes),
            body_size,
        )
        return nodes
    finally:
        doc.close()
