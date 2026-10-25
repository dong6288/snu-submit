import io
import json
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
                                capture_output=True, text=True, timeout=60)
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


if __name__ == '__main__':
    unittest.main()
