"""Opt-in release diagnostics with an isolated database and credential namespace."""
import json
import sys
from pathlib import Path


def smoke(directory):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QTabWidget, QPushButton
    from sales_manager.storage import Store, Secrets
    from sales_manager.ui.app import Window

    destination = Path(directory).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setApplicationName('SalesManagerFirst diagnostics')
    store = Store(destination / 'smoke.sqlite3')
    # Namespace is separate; diagnostics never read a user's API keys or mailbox.
    secrets = Secrets(service='Argon.SalesManagerFirst.Diagnostics')
    window = Window(store=store, secrets=secrets)
    window.show()
    result = {'frozen': bool(getattr(sys, 'frozen', False) or '__compiled__' in globals()),
              'python': sys.version.split()[0], 'executable': sys.executable}
    from sales_manager import __version__
    result['application_version'] = __version__
    try:
        from sales_manager._release_manifest import SOURCE_MANIFEST_SHA256
        result['source_manifest_sha256'] = SOURCE_MANIFEST_SHA256
    except ModuleNotFoundError:
        result['source_manifest_sha256'] = 'unpackaged'
    result['schema_version'] = store.one('PRAGMA user_version')['user_version']
    from PySide6.QtGui import QIcon
    icon_path = Path(__file__).resolve().parents[1] / 'assets' / 'app.ico'
    icon = QIcon(str(icon_path))
    result['application_icon'] = not icon.isNull() and not icon.pixmap(32, 32).isNull()
    ui_root = Path(__file__).resolve().parents[1] / 'ui'
    result['ui_icons'] = all(not QIcon(str(ui_root / name)).pixmap(16, 16).isNull()
                             for name in ('check.svg', 'arrow-down.svg', 'arrow-up.svg'))
    window.setWindowIcon(icon)
    # Exercise certificate resource loading inside the actual frozen bundle, offline.
    import ssl
    tls = window.ai._tls()
    fallback_tls = window.ai._tls(bundled_ca=True)
    result['gigachat_tls'] = all(context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
                                for context in (tls, fallback_tls))
    # Load the actual bundled dictionary, rather than only importing its module.
    from sales_manager.ai.service import _count_noun
    result['russian_inflections'] = (_count_noun('магазинами') == _count_noun('магазинов')
                                      and _count_noun('принтерами') == _count_noun('принтеров')
                                      and _count_noun('принтеров') != _count_noun('сканеров'))

    def capture():
        try:
            tabs = window.findChild(QTabWidget)
            if tabs is None:
                raise RuntimeError('Missing tabs')
            result['tabs'] = [tabs.tabText(i) for i in range(tabs.count())]
            from sales_manager.research.registry import market_source_names
            result['market_source_total'] = len(market_source_names())
            result['market_ui'] = window.statusBar().isHidden() and not any(
                button.text() == 'Статистика импорта' for button in tabs.widget(0).findChildren(QPushButton))
            if result['tabs'] != ['Рынок', 'Компании', 'Настройки']:
                raise RuntimeError('Unexpected tabs')
            for i, name in enumerate(('market', 'companies', 'settings')):
                tabs.setCurrentIndex(i)
                app.processEvents()
                if not window.grab().save(str(destination / (name + '.png'))):
                    raise RuntimeError('Screenshot failed')
            tabs.setCurrentIndex(0)
            tabs.setEnabled(False)
            window.busy_overlay.start('Исследуем рынок. Ищем востребованные товары', window)
            app.processEvents()
            overlay = window.busy_overlay
            from PySide6.QtCore import QPoint
            center = window.mapToGlobal(window.rect().center())
            actual = overlay.geometry().center()
            area = overlay.width() * overlay.height() / (window.width() * window.height())
            first_step = overlay.spinner.step
            from PySide6.QtCore import QEventLoop
            wait = QEventLoop()
            QTimer.singleShot(160, wait.quit)
            wait.exec()
            result['busy_overlay'] = (abs(center.x() - actual.x()) <= 1 and abs(center.y() - actual.y()) <= 1
                                      and .22 < area < .28 and overlay.spinner.timer.isActive()
                                      and overlay.spinner.step != first_step and not tabs.isEnabled())
            # Composite the transparent tool card over its actual main-window image.
            from PySide6.QtGui import QPainter
            screenshot = window.grab()
            painter = QPainter(screenshot)
            position = window.mapFromGlobal(overlay.pos())
            painter.drawPixmap(QPoint(position.x(), position.y()), overlay.grab())
            painter.end()
            screenshot.save(str(destination / 'busy.png'))
            overlay.stop()
            tabs.setEnabled(True)
            result['busy_overlay'] = result['busy_overlay'] and not overlay.isVisible() and not overlay.spinner.timer.isActive()
            result['previous_launch_persistence'] = store.get_setting('diagnostics.persistence') == 'ok'
            store.set_setting('diagnostics.persistence', 'ok')
            store.close()
            reopened = Store(destination / 'smoke.sqlite3')
            result['persistence'] = reopened.get_setting('diagnostics.persistence') == 'ok'
            reopened.close()
            result['ok'] = bool(result['persistence'] and result['gigachat_tls']
                                and result['russian_inflections']
                                and result['application_icon'] and result['ui_icons']
                                and result['market_ui'] and result['busy_overlay'] and result['schema_version'] >= 5)
        except Exception as exc:
            result.update(ok=False, error=type(exc).__name__ + ': ' + str(exc))
        finally:
            (destination / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            window.close()
            app.quit()

    QTimer.singleShot(700, capture)
    app.exec()
    if not result.get('ok'):
        raise SystemExit(1)
