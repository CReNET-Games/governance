import sys
import json
import pytest
from unittest.mock import patch, MagicMock

# Add scripts directory to path to import the script
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../scripts')))

import check_assets

@pytest.fixture
def mock_get_image_files():
    with patch('check_assets.get_image_files') as mock:
        yield mock

@pytest.fixture
def mock_get_ledger_content():
    with patch('check_assets.get_ledger_content') as mock:
        yield mock

def test_no_image_files(mock_get_image_files, capsys):
    mock_get_image_files.return_value = []
    
    with patch('sys.argv', ['check_assets.py', '--mode', 'stop']):
        check_assets.main()
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output == {"decision": "allow"}

def test_image_files_all_logged(mock_get_image_files, mock_get_ledger_content, capsys):
    mock_get_image_files.return_value = ["test1.png", "test2.jpg"]
    mock_get_ledger_content.return_value = '{"file_name": "test1.png"}, {"file_name": "test2.jpg"}'
    
    with patch('sys.argv', ['check_assets.py', '--mode', 'stop']):
        check_assets.main()
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output == {"decision": "allow"}

def test_image_file_missing_stop_mode(mock_get_image_files, mock_get_ledger_content, capsys):
    mock_get_image_files.return_value = ["logged.png", "missing.jpg"]
    mock_get_ledger_content.return_value = '{"file_name": "logged.png"}'
    
    with patch('sys.argv', ['check_assets.py', '--mode', 'stop']):
        check_assets.main()
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["decision"] == "continue"
        assert "missing.jpg" in output["reason"]
        assert "logged.png" not in output["reason"]

def test_image_file_missing_post_invocation_mode(mock_get_image_files, mock_get_ledger_content, capsys):
    mock_get_image_files.return_value = ["missing.png"]
    mock_get_ledger_content.return_value = ''
    
    with patch('sys.argv', ['check_assets.py', '--mode', 'post_invocation']):
        check_assets.main()
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "injectSteps" in output
        assert "missing.png" in output["injectSteps"][0]["ephemeralMessage"]

def test_image_file_missing_claude_mode(mock_get_image_files, mock_get_ledger_content, capsys):
    mock_get_image_files.return_value = ["missing.png"]
    mock_get_ledger_content.return_value = ''
    
    with patch('sys.argv', ['check_assets.py', '--mode', 'claude']):
        with pytest.raises(SystemExit) as excinfo:
            check_assets.main()
        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "missing.png" in captured.err
