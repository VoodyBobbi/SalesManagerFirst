"""Run the real desktop window from the IDE using isolated diagnostic data."""
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == '__main__':
    # Diagnostic screenshots do not need an on-screen window.
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    os.environ['QT_QPA_FONTDIR'] = str(Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts')
    from sales_manager.core.smoke import smoke
    smoke(ROOT / 'build' / 'smoke-ide')
