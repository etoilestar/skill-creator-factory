from backend.services.runtime_tools import read_file_text


def test_read_file_text_txt_and_md(tmp_path, monkeypatch):
    monkeypatch.setenv('INPUT_DIR', str(tmp_path))
    monkeypatch.setenv('OUTPUT_DIR', str(tmp_path / 'out'))
    txt = tmp_path / 'a.txt'; txt.write_text('hello', encoding='utf-8')
    md = tmp_path / 'b.md'; md.write_text('# Title', encoding='utf-8')
    assert read_file_text(str(txt))['text'] == 'hello'
    result = read_file_text(str(md))
    assert result['text'] == '# Title'
    assert result['file_type'] == 'md'
    assert result['metadata'] == {}
