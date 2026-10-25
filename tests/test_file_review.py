import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/snu-submit/skills/snu-submit/scripts'
sys.path.insert(0, str(SCRIPTS))
from submit_checks import parsers, file_review, core


def archive(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, content in entries.items():
            z.writestr(name, content)
    return out.getvalue()


def office(kind, body=None):
    ns = 'http://schemas.openxmlformats.org/'
    if kind == 'docx':
        main, typ = 'word/document.xml', 'wordprocessingml.document'
        body = body or f'<w:document xmlns:w="{ns}wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Conclusion: measured value 7</w:t></w:r></w:p></w:body></w:document>'
        extra = {}
    elif kind == 'pptx':
        main, typ = 'ppt/presentation.xml', 'presentationml.presentation'
        body = body or f'<p:presentation xmlns:p="{ns}presentationml/2006/main" xmlns:r="{ns}officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="r1"/></p:sldIdLst></p:presentation>'
        extra = {
            'ppt/_rels/presentation.xml.rels': f'<Relationships xmlns="{ns}package/2006/relationships"><Relationship Id="r1" Type="{ns}officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/></Relationships>',
            'ppt/slides/slide1.xml': f'<p:sld xmlns:p="{ns}presentationml/2006/main" xmlns:a="{ns}drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Conclusion: measured value 7</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>'}
    else:
        main, typ = 'xl/workbook.xml', 'spreadsheetml.sheet'
        body = body or f'<workbook xmlns="{ns}spreadsheetml/2006/main" xmlns:r="{ns}officeDocument/2006/relationships"><sheets><sheet name="Results" sheetId="1" r:id="r1"/></sheets></workbook>'
        extra = {
            'xl/_rels/workbook.xml.rels': f'<Relationships xmlns="{ns}package/2006/relationships"><Relationship Id="r1" Type="{ns}officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
            'xl/worksheets/sheet1.xml': f'<worksheet xmlns="{ns}spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Conclusion</t></is></c><c r="B1"><f>SUM(3,4)</f><v>7</v></c></row></sheetData></worksheet>'}
    return archive({
        '[Content_Types].xml': f'<Types xmlns="{ns}package/2006/content-types"><Override PartName="/{main}" ContentType="application/vnd.openxmlformats-officedocument.{typ}.main+xml"/></Types>',
        '_rels/.rels': f'<Relationships xmlns="{ns}package/2006/relationships"><Relationship Id="office" Type="{ns}officeDocument/2006/relationships/officeDocument" Target="{main}"/></Relationships>',
        main: body, **extra})


class FormatTests(unittest.TestCase):
    def test_pdf_page_text_is_read_directly_and_inside_zip(self):
        from pypdf import PdfWriter
        from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=100, height=100)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 10 Tf 5 50 Td (Conclusion: value 7) Tj ET')
        page[NameObject('/Contents')] = stream
        data = io.BytesIO()
        writer.write(data)
        for content, name in ((data.getvalue(), 'answer.pdf'), (archive({'answer.pdf': data.getvalue()}), 'answer.zip')):
            parsed = parsers.parse_bytes(content, name)
            self.assertIn('Conclusion: value 7', parsed['text'])
            self.assertTrue(any(loc.get('page') == 1 for loc in parsed['locations']))

    def test_office_text_is_read_directly_and_inside_zip(self):
        for kind in ('docx', 'pptx', 'xlsx'):
            with self.subTest(kind=kind):
                data = office(kind)
                direct = parsers.parse_bytes(data, 'submission.' + kind)
                self.assertEqual(direct['format'], kind)
                self.assertIn('Conclusion', direct['text'] or '')
                nested = parsers.parse_bytes(archive({'submission.' + kind: data}), 'submission.zip')
                self.assertIn('Conclusion', nested['text'] or '')
                self.assertFalse(nested['archive_entries'][0]['nested_archive'])

    def test_excel_cells_formulas_and_slide_locations_are_visible(self):
        result = parsers.parse_bytes(office('xlsx'), 'results.xlsx')
        self.assertIn('SUM(3,4)', result['text'])
        self.assertTrue(any('cell:B1' in loc['label'] for loc in result['locations']))
        self.assertTrue(any('not recalculated' in region['detail'] for region in result['unread_regions']))
        slides = parsers.parse_bytes(office('pptx'), 'slides.pptx')
        self.assertEqual(slides['pages'], 1)
        self.assertTrue(any('slide:1' in loc['label'] for loc in slides['locations']))
        core._fragment_validator('checks.json', 'parser').validate(core._public_parser(slides))

    def test_python_is_read_and_checked_without_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'executed.txt'
            program = f'from pathlib import Path\nPath({str(marker)!r}).write_text("executed")\n'
            result = parsers.parse_bytes(program.encode(), 'solution.py')
            self.assertEqual(result['status'], 'complete')
            self.assertFalse(marker.exists())
            broken = parsers.parse_bytes(b'def incomplete(:\n', 'solution.py')
            self.assertEqual(broken['status'], 'failed')
            self.assertIn('syntax error', broken['reason'])

    def test_office_limits_and_malformed_packages_do_not_pass(self):
        for data, name in ((b'not a pdf', 'x.pdf'), (office('xlsx'), 'x.pptx'),
                           (archive({'[Content_Types].xml': '<bad>'}), 'x.xlsx')):
            with self.subTest(name=name):
                result = parsers.parse_bytes(data, name)
                self.assertNotEqual(result['status'], 'complete')
                self.assertTrue(any(r['kind'] == 'parse_error' for r in result['unread_regions']))
        nested = parsers.parse_bytes(archive({'x.docx': office('docx')}), 'x.zip', {'max_uncompressed_bytes': 100})
        self.assertNotEqual(nested['status'], 'complete')

    def test_office_auxiliary_xml_is_validated_and_layout_content_is_limited(self):
        with zipfile.ZipFile(io.BytesIO(office('pptx'))) as z:
            entries = {name: z.read(name) for name in z.namelist()}
        ns = 'http://schemas.openxmlformats.org/'
        entries['ppt/slides/_rels/slide1.xml.rels'] = f'<Relationships xmlns="{ns}package/2006/relationships"><Relationship Id="layout" Type="{ns}officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/></Relationships>'
        entries['ppt/slideLayouts/slideLayout1.xml'] = '<broken'
        broken = parsers.parse_bytes(archive(entries), 'slides.pptx')
        self.assertNotEqual(broken['status'], 'complete')
        self.assertTrue(any(r['kind'] == 'parse_error' for r in broken['unread_regions']))
        entries['ppt/slideLayouts/slideLayout1.xml'] = f'<p:sldLayout xmlns:p="{ns}presentationml/2006/main"/>'
        limited = parsers.parse_bytes(archive(entries), 'slides.pptx')
        self.assertEqual(limited['status'], 'incomplete')
        self.assertTrue(any('slideLayout' in r['detail'] for r in limited['unread_regions']))
        self.assertFalse(any(r['kind'] == 'parse_error' for r in limited['unread_regions']))

    def test_relationship_resolution_stays_inside_package(self):
        self.assertEqual(parsers._ooxml_resolve('ppt/slides/', '../slideLayouts/layout.xml'), 'ppt/slideLayouts/layout.xml')
        self.assertIsNone(parsers._ooxml_resolve('ppt/slides/', '../../../outside.xml'))
        root = parsers.ET.fromstring(f'<Relationships xmlns="{parsers.NS_REL[1:-1]}"><Relationship Id="r" Type="image" Target="image.png"/></Relationships>')
        self.assertEqual(parsers._ooxml_relationship_problems('_rels/document.xml.rels', root, {'document.xml', 'image.png'}), [])


def requirement(pattern, literals=None):
    return {'id': 'artifact', 'description': 'Required synthetic artifact', 'evidence': 'Synthetic assignment instruction',
            'patterns': [pattern], 'min_count': 1, 'max_count': None, 'text_contains': literals or []}


def profile(pattern, literals=None, limits=None):
    return {'profile_version': 'file-review/1', 'requirements': [requirement(pattern, literals)], 'source_limits': limits or []}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def review(self, name, data, rules):
        path = self.root / name
        path.write_bytes(data)
        report = file_review.inspect_files([str(path)], rules)
        self.assertEqual(path.read_bytes(), data)
        return report

    def test_native_candidates_get_limits_not_full_pass(self):
        rules = profile('work.zip!database/*.CDB')
        rules['requirements'].append(dict(requirement('work.zip!results/*.FIG'), id='figure'))
        result = self.review('work.zip', archive({'database/final.CDB': b'\x00\x01data', 'results/plot.FIG': b'\x00native'}), rules)
        self.assertEqual(result['verdict'], 'PASS_WITH_LIMITS')
        self.assertEqual(result['checks'][0]['matched_paths'], ['work.zip!database/final.CDB'])
        self.assertTrue(all(item['sha256'] for item in result['files']))

    def test_missing_empty_and_bad_files_never_get_qualified_pass(self):
        cases = [({'answer.txt': b'ok'}, profile('work.zip!missing.fig'), 'NEEDS_CHANGES'),
                 ({'plot.fig': b''}, profile('work.zip!plot.fig'), 'NEEDS_CHANGES'),
                 ({'answer.txt': b'  \n'}, profile('work.zip!answer.txt'), 'NEEDS_CHANGES'),
                 ({'x.pdf': b'not PDF'}, profile('work.zip!x.pdf'), 'NEEDS_CONFIRMATION'),
                 ({'../x.fig': b'native'}, profile('work.zip!*'), 'NEEDS_CONFIRMATION'),
                 ({'x.zip': archive({'x.txt': b'content'})}, profile('work.zip!*'), 'NEEDS_CONFIRMATION')]
        for entries, rules, expected in cases:
            with self.subTest(entries=list(entries)):
                self.assertEqual(self.review('work.zip', archive(entries), rules)['verdict'], expected)

    def test_supported_content_and_missing_required_text(self):
        for kind in ('docx', 'pptx', 'xlsx'):
            with self.subTest(kind=kind):
                rules = profile('work.zip!answer.' + kind, ['Conclusion'])
                report = self.review('work.zip', archive({'answer.' + kind: office(kind)}), rules)
                self.assertIn(report['verdict'], ('PASS', 'PASS_WITH_LIMITS'))
                self.assertEqual(report['checks'][0]['missing_literals'], [])
        report = self.review('answer.txt', b'Introduction only', profile('answer.txt', ['Conclusion']))
        self.assertEqual(report['verdict'], 'NEEDS_CHANGES')

    def test_source_gap_is_explicit_and_does_not_hide_file_defects(self):
        rules = profile('answer.txt', limits=['Assignment attachment could not be downloaded'])
        result = self.review('answer.txt', b'Complete text', rules)
        self.assertEqual(result['verdict'], 'PASS_WITH_LIMITS')
        self.assertEqual(result['source_limits'], rules['source_limits'])
        self.assertEqual(self.review('answer.txt', b'', rules)['verdict'], 'NEEDS_CHANGES')

    def test_changed_or_missing_input_blocks_pass(self):
        from unittest.mock import patch
        path = self.root / 'answer.txt'
        path.write_text('answer')
        original = file_review.read_file_bound
        calls = []
        def changing(*args):
            calls.append(args)
            if len(calls) == 2:
                path.write_text('changed')
            return original(*args)
        with patch.object(file_review, 'read_file_bound', side_effect=changing):
            report = file_review.inspect_files([str(path)], profile('answer.txt'))
        self.assertEqual(report['verdict'], 'NEEDS_CONFIRMATION')
        self.assertTrue(report['failures'])

    def test_cli_writes_reports_and_escapes_untrusted_text(self):
        path = self.root / 'answer.py'
        path.write_text('text = "<script>alert(1)</script>"\n', encoding='utf-8')
        rules = self.root / 'profile.json'
        rules.write_text(json.dumps(profile('answer.py', ['text ='])))
        out = self.root / 'output'
        result = subprocess.run([sys.executable, str(SCRIPTS / 'snu_submit.py'), 'review', '--file', str(path),
                                 '--profile', str(rules), '--output', str(out), '--include-content'],
                                capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((out / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['verdict'], 'PASS')
        page = (out / 'report.html').read_text(encoding='utf-8')
        self.assertNotIn('<script>', page)
        self.assertIn('&lt;script&gt;', page)

    def test_empty_profile_is_rejected(self):
        with self.assertRaises(core.ValidationError):
            file_review.validate_profile({'profile_version': 'file-review/1', 'requirements': [], 'source_limits': []})

    def test_empty_archive_is_a_missing_deliverable(self):
        self.assertEqual(self.review('empty.zip', archive({}), profile('empty.zip'))['verdict'], 'NEEDS_CHANGES')
        self.assertEqual(self.review('empty.zip', archive({'folder/': b''}), profile('empty.zip'))['verdict'], 'NEEDS_CHANGES')

    def test_unsafe_office_member_is_not_a_content_limit(self):
        for kind in ('docx', 'pptx', 'xlsx'):
            with zipfile.ZipFile(io.BytesIO(office(kind))) as z:
                entries = {name: z.read(name) for name in z.namelist()}
            entries['../unsafe.txt'] = b'untrusted'
            self.assertEqual(self.review('answer.' + kind, archive(entries), profile('answer.' + kind))['verdict'], 'NEEDS_CONFIRMATION')
            nested = archive({'answer.' + kind: archive(entries)})
            self.assertEqual(self.review('submission.zip', nested, profile('submission.zip!answer.' + kind))['verdict'], 'NEEDS_CONFIRMATION')

    def test_corrupt_docx_header_is_not_a_content_limit(self):
        with zipfile.ZipFile(io.BytesIO(office('docx'))) as z:
            entries = {name: z.read(name) for name in z.namelist()}
        entries['word/header1.xml'] = '<broken'
        self.assertEqual(self.review('report.docx', archive(entries), profile('report.docx'))['verdict'], 'NEEDS_CONFIRMATION')

    def test_missing_or_escaping_office_relationship_never_passes(self):
        ns = 'http://schemas.openxmlformats.org/'
        for kind, part, target in (('docx', 'word/_rels/document.xml.rels', 'missing.xml'),
                                   ('pptx', 'ppt/slides/_rels/slide1.xml.rels', '../slideLayouts/missing.xml'),
                                   ('xlsx', 'xl/worksheets/_rels/sheet1.xml.rels', '../drawings/missing.xml')):
            for linked in (target, '../../../outside.xml'):
                with self.subTest(kind=kind, target=linked):
                    with zipfile.ZipFile(io.BytesIO(office(kind))) as z:
                        entries = {name: z.read(name) for name in z.namelist()}
                    entries[part] = f'<Relationships xmlns="{ns}package/2006/relationships"><Relationship Id="aux" Type="{ns}officeDocument/2006/relationships/slideLayout" Target="{linked}"/></Relationships>'
                    data = archive(entries)
                    name = 'answer.' + kind
                    self.assertEqual(self.review(name, data, profile(name))['verdict'], 'NEEDS_CONFIRMATION')
                    self.assertEqual(self.review('work.zip', archive({name: data}), profile('work.zip!' + name))['verdict'], 'NEEDS_CONFIRMATION')

    def test_unvisited_auxiliary_relationship_is_validated(self):
        with zipfile.ZipFile(io.BytesIO(office('pptx'))) as z:
            entries = {name: z.read(name) for name in z.namelist()}
        ns = 'http://schemas.openxmlformats.org/'
        entries['ppt/slideLayouts/layout.xml'] = f'<p:sldLayout xmlns:p="{ns}presentationml/2006/main"/>'
        entries['ppt/slideLayouts/_rels/layout.xml.rels'] = f'<Relationships xmlns="{ns}package/2006/relationships"><Relationship Id="image" Type="{ns}officeDocument/2006/relationships/image" Target="../media/missing.png"/></Relationships>'
        self.assertEqual(self.review('answer.pptx', archive(entries), profile('answer.pptx'))['verdict'], 'NEEDS_CONFIRMATION')


W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def docx_body(body_xml):
    """A minimal DOCX whose w:body is body_xml (Word namespace prefix w, math prefix m)."""
    return office('docx', f'<w:document xmlns:w="{W}" xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">'
                          f'<w:body>{body_xml}</w:body></w:document>')


def paras(*texts):
    return ''.join(f'<w:p><w:r><w:t xml:space="preserve">{t}</w:t></w:r></w:p>' for t in texts)


IMAGE_PARA = '<w:p><w:r><w:drawing><pic xmlns="urn:x"><blip/></pic></w:drawing></w:r></w:p>'


def pdf_pages(*pages):
    """PDF with one page per item: a text string, None (blank page) or 'IMAGE' (image only)."""
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject
    writer = PdfWriter()
    for content in pages:
        page = writer.add_blank_page(width=200, height=200)
        if content is None:
            continue
        stream = DecodedStreamObject()
        if content == 'IMAGE':
            image = DecodedStreamObject()
            image.set_data(b'\x00')
            image.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'),
                          NameObject('/Width'): NumberObject(1), NameObject('/Height'): NumberObject(1),
                          NameObject('/ColorSpace'): NameObject('/DeviceGray'), NameObject('/BitsPerComponent'): NumberObject(8)})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/XObject'): DictionaryObject(
                {NameObject('/Im0'): writer._add_object(image)})})
            stream.set_data(b'q 100 0 0 100 0 0 cm /Im0 Do Q')
        else:
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                     NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
            lines = content.split('\n')
            ops = ['BT /F1 10 Tf 12 TL 10 180 Td'] + [f'({line}) Tj T*' for line in lines] + ['ET']
            stream.set_data(' '.join(ops).encode('latin-1'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    data = io.BytesIO()
    writer.write(data)
    return data.getvalue()


def hwpx(paragraph_xml):
    hp = 'http://www.hancom.co.kr/hwpml/2011/paragraph'
    section = f'<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" xmlns:hp="{hp}">{paragraph_xml}</hs:sec>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr('mimetype', 'application/hwp+zip')
        z.writestr('Contents/section0.xml', section)
        z.writestr('Contents/header.xml', '<hh:head xmlns:hh="urn:h"/>')
    return out.getvalue()


def req(rid, patterns, **extra):
    return dict({'id': rid, 'description': rid, 'evidence': 'synthetic instruction', 'patterns': patterns}, **extra)


def rules(*rows, limits=None):
    return {'profile_version': 'file-review/1', 'requirements': list(rows), 'source_limits': limits or []}


class ParserAdditionsTests(unittest.TestCase):
    def test_blank_and_image_only_pdf_pages_are_told_apart(self):
        blank = parsers.parse_bytes(pdf_pages(None, None), 'blank.pdf')
        self.assertEqual((blank['pages'], blank['metrics']['blank_pages']), (2, 2))
        mixed = parsers.parse_bytes(pdf_pages('Problem 1', 'IMAGE', None), 'mixed.pdf')
        self.assertEqual(mixed['metrics']['blank_pages'], 1)
        self.assertTrue(any(r['kind'] == 'scanned_page' and r['location'] == {'page': 2} for r in mixed['unread_regions']))

    def test_known_foreign_signature_is_a_format_mismatch_also_inside_zip(self):
        direct = parsers.parse_bytes(office('docx'), 'report.pdf')
        self.assertEqual([r['kind'] for r in direct['unread_regions']], ['format_mismatch'])
        nested = parsers.parse_bytes(archive({'report.pdf': office('docx'), 'notes.txt': b'\x89PNG\r\n\x1a\nxx'}), 'hw.zip')
        self.assertEqual(sorted(r['kind'] for r in nested['unread_regions']), ['format_mismatch', 'format_mismatch'])
        unsigned = parsers.parse_bytes(b'not a pdf at all', 'report.pdf')
        self.assertEqual([r['kind'] for r in unsigned['unread_regions']], ['parse_error'])

    def test_docx_equation_text_is_kept(self):
        body = ('<w:p><w:r><w:t>Answer: </w:t></w:r><m:oMath><m:r><m:t>x=2</m:t></m:r></m:oMath></w:p>')
        result = parsers.parse_bytes(docx_body(body), 'math.docx')
        self.assertIn('x=2', result['text'])
        self.assertTrue(any('equation text' in r['detail'] for r in result['unread_regions']))

    def test_hwpx_paragraphs_tables_and_pictures(self):
        body = ('<hp:p><hp:run><hp:t>1. 서론</hp:t></hp:run></hp:p>'
                '<hp:p><hp:run><hp:t>본문<hp:tab/>내용</hp:t></hp:run></hp:p>'
                '<hp:p><hp:run><hp:tbl><hp:tr><hp:tc><hp:subList><hp:p><hp:run><hp:t>표 칸</hp:t></hp:run></hp:p>'
                '</hp:subList></hp:tc></hp:tr></hp:tbl></hp:run></hp:p>'
                '<hp:p><hp:run><hp:pic/></hp:run></hp:p>')
        result = parsers.parse_bytes(hwpx(body), 'report.hwpx')
        self.assertEqual(result['format'], 'hwpx')
        self.assertEqual(result['text'].split('\n'), ['1. 서론', '본문\t내용', '표 칸'])
        self.assertEqual(result['metrics']['tables'], 1)
        self.assertTrue(any(r['kind'] == 'image' and r['location'] == {'paragraph': 5} for r in result['unread_regions']))
        inside = parsers.parse_bytes(archive({'report.hwpx': hwpx(body)}), 'hw.zip')
        self.assertFalse(inside['archive_entries'][0]['nested_archive'])
        self.assertIn('표 칸', inside['text'])
        not_hwpx = archive({'Contents/section0.xml': '<x/>'})
        self.assertEqual(parsers.parse_bytes(not_hwpx, 'fake.hwpx')['status'], 'failed')


class ReviewVerdictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def review(self, files, profile):
        paths = []
        for name, data in files.items():
            path = self.root / name
            path.write_bytes(data)
            paths.append(str(path))
        report = file_review.inspect_files(paths, profile)
        for name, data in files.items():
            self.assertEqual((self.root / name).read_bytes(), data)
        return report

    def test_missing_literal_is_a_violation_only_when_everything_was_read(self):
        clean = self.review({'report.docx': docx_body(paras('1. 서론', '내용'))}, rules(req('r', ['report.docx'], text_contains=['결론'])))
        self.assertEqual(clean['verdict'], 'NEEDS_CHANGES')
        with_image = self.review({'report.docx': docx_body(paras('1. 서론', '내용') + IMAGE_PARA)},
                                 rules(req('r', ['report.docx'], text_contains=['결론'])))
        self.assertEqual(with_image['verdict'], 'NEEDS_CONFIRMATION')

    def test_unmatched_or_extra_empty_files_do_not_fail_the_submission(self):
        files = {'hw.zip': archive({'main.py': 'print(1)\n', 'pkg/__init__.py': '', '__MACOSX/._main.py': b'\x00\x05\x16\x07'})}
        self.assertEqual(self.review(files, rules(req('code', ['hw.zip!main.py'])))['verdict'], 'PASS')
        report = self.review(files, rules(req('code', ['hw.zip!*.py'])))
        self.assertEqual(report['verdict'], 'PASS')
        self.assertTrue(any(f['status'] == 'warning' for f in report['checks'][0]['findings']))
        self.assertNotIn('hw.zip!__MACOSX/._main.py', report['checks'][0]['matched_paths'])

    def test_missing_file_is_a_violation_even_when_another_file_is_unreadable(self):
        report = self.review({'report.pdf': b'%PDF-1.4 broken'}, rules(req('report', ['report.pdf']), req('appendix', ['appendix.zip'])))
        self.assertEqual(report['verdict'], 'NEEDS_CHANGES')
        self.assertEqual([c['status'] for c in report['checks']], ['unknown', 'fail'])

    def test_missing_entry_inside_an_unreadable_archive_is_unknown(self):
        report = self.review({'hw.zip': b'PK\x03\x04 broken archive'}, rules(req('code', ['hw.zip!main.py'])))
        self.assertEqual(report['verdict'], 'NEEDS_CONFIRMATION')
        report = self.review({'hw.7z': b'7z\xbc\xaf\x27\x1c rest'}, rules(req('code', ['hw.7z!main.py'])))
        self.assertEqual(report['verdict'], 'NEEDS_CONFIRMATION')

    def test_blank_pdf_and_renamed_document_are_violations(self):
        self.assertEqual(self.review({'report.pdf': pdf_pages(None)}, rules(req('r', ['report.pdf'])))['verdict'], 'NEEDS_CHANGES')
        renamed = self.review({'report.pdf': office('docx')}, rules(req('r', ['report.pdf'])))
        self.assertEqual(renamed['verdict'], 'NEEDS_CHANGES')
        self.assertIn('형식 오류', renamed['checks'][0]['findings'][0]['message'])

    def test_scanned_pages_give_a_limited_pass_with_manual_check(self):
        report = self.review({'report.pdf': pdf_pages('Problem 1 answer', 'IMAGE')}, rules(req('r', ['report.pdf'])))
        self.assertEqual(report['verdict'], 'PASS_WITH_LIMITS')
        self.assertTrue(any('2쪽' in line for line in report['manual_checks']))

    def test_format_name_and_page_rules(self):
        files = {'report.docx': docx_body(paras('본문'))}
        self.assertEqual(self.review(files, rules(req('r', ['report.*'], formats=['pdf'])))['verdict'], 'NEEDS_CHANGES')
        files = {'hw3.pdf': pdf_pages('a', 'b', 'c')}
        self.assertEqual(self.review(files, rules(req('r', ['*.pdf'], name_globs=['*_*_HW3.pdf'])))['verdict'], 'NEEDS_CHANGES')
        self.assertEqual(self.review(files, rules(req('r', ['*.pdf'], pages={'max': 2})))['verdict'], 'NEEDS_CHANGES')
        self.assertEqual(self.review(files, rules(req('r', ['*.pdf'], pages={'max': 3})))['verdict'], 'PASS')
        docx = {'r.docx': docx_body(paras('본문'))}
        self.assertEqual(self.review(docx, rules(req('r', ['r.docx'], pages={'max': 3})))['verdict'], 'NEEDS_CONFIRMATION')

    def test_sections_detect_empty_answers_but_not_table_of_contents(self):
        sections = [{'heading': '1. 구현 방법', 'until': '2. 실행 결과'}, {'heading': '2. 실행 결과', 'until': '3. 결론'},
                    {'heading': '3. 결론', 'until': None}]
        toc = paras('목차', '1. 구현 방법 ........ 1', '2. 실행 결과 ........ 2', '3. 결론 ........ 3')
        filled = docx_body(toc + paras('1. 구현 방법', '병합 정렬', '2. 실행 결과', '0.3초', '3. 결론', '빠르다'))
        report = self.review({'r.docx': filled}, rules(req('r', ['r.docx'], sections=sections)))
        self.assertEqual(report['verdict'], 'PASS', report['checks'][0]['findings'])
        empty = docx_body(toc + paras('1. 구현 방법', '병합 정렬', '2. 실행 결과', '3. 결론', '빠르다'))
        report = self.review({'r.docx': empty}, rules(req('r', ['r.docx'], sections=sections)))
        self.assertEqual(report['verdict'], 'NEEDS_CHANGES')
        failed = [s for s in report['checks'][0]['sections'] if s['status'] == 'fail']
        self.assertEqual([s['heading'] for s in failed], ['2. 실행 결과'])
        self.assertEqual(failed[0]['where'], '문단 7')
        image_only = docx_body(paras('1. 구현 방법', '병합 정렬', '2. 실행 결과') + IMAGE_PARA + paras('3. 결론', '빠르다'))
        self.assertEqual(self.review({'r.docx': image_only}, rules(req('r', ['r.docx'], sections=sections)))['verdict'],
                         'NEEDS_CONFIRMATION')

    def test_inline_answer_markers_and_points_beside_headings(self):
        doc = docx_body(paras('문제 1 (10점)', '문제 2 (10점)', '답: 42'))
        sections = [{'heading': '문제 1', 'until': '문제 2'}, {'heading': '답:', 'until': None}]
        report = self.review({'a.docx': doc}, rules(req('a', ['a.docx'], sections=sections)))
        self.assertEqual([s['status'] for s in report['checks'][0]['sections']], ['fail', 'pass'])

    def test_unsafe_archive_entry_blocks_pass_even_if_requirements_match(self):
        files = {'hw.zip': archive({'report.txt': 'done', '../evil.txt': 'x'})}
        self.assertEqual(self.review(files, rules(req('r', ['hw.zip!report.txt'])))['verdict'], 'NEEDS_CONFIRMATION')

    def test_unmatched_empty_top_level_file_needs_confirmation(self):
        files = {'report.txt': b'done', 'extra.txt': b''}
        report = self.review(files, rules(req('r', ['report.txt'])))
        self.assertEqual(report['verdict'], 'NEEDS_CONFIRMATION')
        self.assertTrue(report['blocking'])

    def test_profile_defaults_and_validation(self):
        normalized = file_review.validate_profile({'profile_version': 'file-review/1',
                                                   'requirements': [{'id': 'a', 'description': 'd', 'evidence': 'e', 'patterns': ['*.pdf']}]})
        self.assertEqual((normalized['requirements'][0]['min_count'], normalized['source_limits']), (1, []))
        for bad in ({'formats': ['p d f']}, {'name_globs': ['dir/x.pdf']}, {'pages': {'max': 0}},
                    {'sections': [{'heading': 'a\nb'}]}, {'patterns': ['x.zip!../y']}, {'unknown': 1}):
            with self.subTest(bad=bad), self.assertRaises(core.ValidationError):
                file_review.validate_profile(rules(dict(req('a', ['*.pdf']), **bad)))


class InspectAndCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONIOENCODING'}  # piped output must default to UTF-8
        return subprocess.run([sys.executable, str(SCRIPTS / 'snu_submit.py'), *args], capture_output=True, text=True,
                              timeout=120, cwd=self.root, encoding='utf-8', env=env)

    def test_inspect_writes_default_run_folder_and_shows_text(self):
        source = self.root / 'in' / 'hw.zip'
        source.parent.mkdir()
        source.write_bytes(archive({'report.docx': docx_body(paras('데이터구조 과제 3', '풀이'))}))
        result = self.run_cli('inspect', '--file', str(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('데이터구조 과제 3', result.stdout)
        runs = list((self.root / '.snu-submit' / 'runs').iterdir())
        self.assertEqual(len(runs), 1)
        data = json.loads((runs[0] / 'inspection.json').read_text(encoding='utf-8'))
        self.assertIn('풀이', next(f['text'] for f in data['files'] if f['path'] == 'hw.zip!report.docx'))

    def test_review_previous_delta_and_korean_summary(self):
        target = self.root / 'answer.txt'
        target.write_text('first', encoding='utf-8')
        profile_path = self.root / 'profile.json'
        profile_path.write_text(json.dumps(rules(req('a', ['answer.txt'], text_contains=['final']))), encoding='utf-8')
        first = self.run_cli('review', '--file', str(target), '--profile', str(profile_path), '--output', str(self.root / 'r1'))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('판정: 수정 필요', first.stdout)
        target.write_text('final answer', encoding='utf-8')
        second = self.run_cli('review', '--file', str(target), '--profile', str(profile_path), '--output', str(self.root / 'r2'),
                              '--previous', str(self.root / 'r1' / 'report.json'))
        self.assertEqual(second.returncode, 0, second.stderr)
        report = json.loads((self.root / 'r2' / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['verdict'], 'PASS')
        self.assertEqual(report['delta']['files_changed'], ['answer.txt'])
        self.assertEqual(report['delta']['requirement_changes'], [{'id': 'a', 'previous': 'fail', 'current': 'pass'}])
        self.assertNotIn('text', report['files'][0])


if __name__ == '__main__':
    unittest.main()
