"""Desktop workflow tests; network services and secrets are isolated here only."""
import threading

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QStyle, QStyleOptionButton

from sales_manager.storage import Store, now
from sales_manager.ui import app
from sales_manager.ui import dialogs
from sales_manager.ui.dialogs import CompanyDialog, HistoryDialog, OfferDialog, SourcesDialog


class MemorySecrets:
    def __init__(self):
        self.values = {}

    def get(self, key, default=''):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


class StubService:
    def __init__(self, *args):
        pass

    def close(self):
        pass

    def pending_opt_outs(self):
        return []


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'ResearchService', StubService)
    monkeypatch.setattr(app, 'AIService', StubService)
    monkeypatch.setattr(app, 'MailService', StubService)
    store = Store(tmp_path / 'ui.sqlite3')
    view = app.Window(store=store, secrets=MemorySecrets())
    view.timer.stop()
    view.startup_mail_timer.stop()
    qtbot.addWidget(view)
    view.show()
    qtbot.waitUntil(lambda: not view.workers, timeout=5000)
    yield view
    qtbot.waitUntil(lambda: not view.workers, timeout=5000)
    view.close()


def company(store, product_id, name='Компания', email='info@buyer.ru', phone='+79991234567', manual=0):
    source_id = store.put_source('https://example.org/contacts', 'Контакты', 'Производство мебели. Контакты компании.')
    company_id = store.execute('INSERT INTO companies(name,domain,url,country,created_at) VALUES(?,?,?,?,?)', (name, 'example.org', 'https://example.org', 'Россия', now()))
    store.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids,manual,checked_at) VALUES(?,?,?,?,?,?,?)', (product_id, company_id, 'FIT', 'Применение товара в производстве.', '[' + str(source_id) + ']', manual, now()))
    for kind, value in [('email', email), ('phone', phone)]:
        if value:
            store.execute('INSERT INTO company_contacts(company_id,kind,value,source_id,checked_at) VALUES(?,?,?,?,?)', (company_id, kind, value, source_id, now()))
    return company_id


def offer(store, product_id, company_id, body='Проверенный человеком текст'):
    return store.execute('INSERT INTO offers(product_id,company_id,subject,body,edited,created_at,updated_at) VALUES(?,?,?,?,1,?,?)', (product_id, company_id, 'Предложение', body, now(), now()))


def test_three_tabs_and_independent_product_branches(window):
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == ['Рынок', 'Компании', 'Настройки']
    assert [window.company_table.horizontalHeaderItem(i).text() for i in range(6)] == ['Компания', 'Email', 'Телефон', 'УТП', 'Ответ', 'История']
    first = window.store.ensure_product('Древесина')['id']
    second = window.store.ensure_product('Картон')['id']
    company(window.store, first, name='Первый клиент')
    company(window.store, second, name='Второй клиент')
    window.open_product(first)
    assert [r['name'] for r in window.company_rows] == ['Первый клиент']
    window.open_product(second)
    assert [r['name'] for r in window.company_rows] == ['Второй клиент']
    window.open_product(first)
    assert window.store.get_setting('product.branches') == [first, second]
    assert len(window.company_rows) == 1


def test_import_statistics_dialog_is_separate_and_shows_period(window, monkeypatch, qtbot):
    from sales_manager.research.providers import ComtradeProvider
    from sales_manager.research.service import ResearchService
    year = '2020'
    provider = ComtradeProvider(lambda *args, **kw: {'data': [{
        'reporterCode': 643, 'period': year, 'cmdCode': '0901', 'flowCode': 'M',
        'partnerCode': 0, 'primaryValue': 100.0}]}, year=year)
    service = ResearchService(window.store, window.secrets)
    service._market_evidence(provider.search('кофе', 'Россия')[0], lambda _: None)
    captured = {}
    class Dialog:
        def __init__(self, rows, parent):
            captured['rows'] = rows
        def setWindowTitle(self, title):
            captured['title'] = title
        def exec(self):
            captured['shown'] = True
    monkeypatch.setattr(app, 'SourcesDialog', Dialog)
    window.countries = ['Россия']
    window.refresh_market()
    qtbot.waitUntil(lambda: not window.workers)
    assert window.market_rows == []
    window.show_import_statistics()
    assert captured['shown'] and 'отдельная статистика' in captured['title']
    assert captured['rows'][0]['period'] == year
    assert year in captured['rows'][0]['fact'] and 'Импорт' in captured['rows'][0]['fact']


@pytest.mark.parametrize('kind', ['market', 'companies'])
def test_all_sources_exhausted_finishes_without_resume_control(window, qtbot, monkeypatch, kind):
    from unittest.mock import Mock
    from sales_manager.research.providers import ProviderError
    from sales_manager.research.service import ResearchService
    service = ResearchService(window.store, window.secrets)
    window.research = service
    window.countries = ['Россия']
    product_id = None
    if kind == 'companies':
        product_id = window.store.ensure_product('кофе')['id']
        window.open_product(product_id)
    task = {'phase': 'search', 'provider': 'gdelt', 'query': 'кофе', 'country': 'Россия', 'market': kind == 'market'}
    unavailable = Mock(search=Mock(side_effect=ProviderError('Нет ответа')))
    first = service._run(kind, product_id, ['Россия'], [task], {'gdelt': unavailable}, lambda _: None, None)
    assert first['status'] == 'complete'
    run = window.store.one('SELECT status FROM job_runs WHERE id=?', (first['run_id'],))
    work = window.store.one('SELECT status,error FROM job_queue WHERE run_id=?', (first['run_id'],))
    assert run['status'] == 'complete'
    assert work['status'] == 'done' and work['error']
    assert window.market_pause.isHidden() and window.company_pause.isHidden()


def test_custom_product_is_entered_in_companies_without_starting_search(window, qtbot, monkeypatch):
    calls = []
    def find(product_id, countries, emit, cancel, manual_url=None):
        calls.append(product_id)
        return {'status': 'complete'}
    monkeypatch.setattr(window.research, 'find_companies', find, raising=False)
    window.tabs.setCurrentIndex(1)
    window.branches.setEditText('Упаковочная бумага')
    qtbot.keyClick(window.branches.lineEdit(), Qt.Key.Key_Return)
    assert calls == []
    assert window.store.one('SELECT name FROM products WHERE id=?', (window.product_id,))['name'] == 'Упаковочная бумага'
    assert window.tabs.currentIndex() == 1
    assert window.company_start.isEnabled()
    qtbot.mouseClick(window.company_start, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not window.workers, timeout=3000)
    assert calls == [window.product_id]


def test_find_companies_resumes_saved_manual_run(window, qtbot, monkeypatch):
    import json
    product = window.store.ensure_product('Кофе')
    window.open_product(product['id'])
    run_id = window.store.execute('INSERT INTO job_runs(kind,product_id,status,countries,created_at,updated_at,message) VALUES(?,?,?,?,?,?,?)',
        ('manual:abc', product['id'], 'paused', json.dumps(window.countries, ensure_ascii=False), now(), now(), 'Приостановлено'))
    window.store.execute('INSERT INTO job_runs(kind,product_id,status,countries,created_at,updated_at,message) VALUES(?,?,?,?,?,?,?)',
        ('manual:def', product['id'], 'complete', json.dumps(window.countries, ensure_ascii=False), now(), now(), 'Завершено'))
    calls = []
    monkeypatch.setattr(window.research, 'resume_companies', lambda rid, pid, countries, emit, cancel: calls.append((rid, pid)) or {'status': 'complete'}, raising=False)
    monkeypatch.setattr(window.research, 'find_companies', lambda pid,countries,emit,cancel,manual_url=None: calls.append(('ordinary',pid)) or {'status':'complete'}, raising=False)
    monkeypatch.setattr(window.research, 'available_sources', lambda kind: ['rss'], raising=False)
    window.restore_research_notices()
    assert 'автоматического восстановления' in window.company_research_notice.text()
    qtbot.mouseClick(window.company_start, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not window.workers, timeout=3000)
    assert calls == [('ordinary', product['id'])]
    assert window.store.one('SELECT status FROM job_runs WHERE id=?',(run_id,))['status'] == 'paused'


def test_market_selection_opens_branch_without_search(window, qtbot, monkeypatch):
    product = window.store.ensure_product('Картон')
    monkeypatch.setattr(app, 'market_ranking', lambda *args: {'rows': [dict(id=product['id'], name='Картон', basis='Продажи: 100 шт. · 2026-11 · Россия', count=2, source_ids=[1, 2])], 'preliminary': False})
    window.refresh_market()
    qtbot.waitUntil(lambda: window.market_table.rowCount() == 1)
    calls = []
    monkeypatch.setattr(window, 'start_companies', lambda: calls.append(window.product_id))
    rect = window.market_table.visualItemRect(window.market_table.item(0, 0))
    qtbot.mouseClick(window.market_table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert calls == []
    assert window.product_id == product['id']
    assert window.tabs.currentIndex() == 1
    assert window.store.get_setting('product.branches') == [product['id']]


def test_market_demand_percentage_colors_are_subtle_and_unknown_uncolored(window, monkeypatch, qtbot):
    rows = [dict(id=i, name=str(i), basis='факт', demand_percent=value) for i, value in enumerate((0, 50, 100, None), 1)]
    monkeypatch.setattr(app, 'market_ranking', lambda *args: {'rows': rows, 'preliminary': True})
    window.refresh_market()
    qtbot.waitUntil(lambda: window.market_table.rowCount() == 4)
    cells = [window.market_table.item(i, 1) for i in range(4)]
    assert [cell.text() for cell in cells] == ['0%', '50%', '100%', 'факт']
    low, mid, high = [cell.background().color() for cell in cells[:3]]
    assert low.blue() > low.red() and high.red() > high.blue()
    assert low.red() < mid.red() < high.red()
    assert max(low.red(), low.green(), low.blue(), high.red(), high.green(), high.blue()) <= 80
    assert cells[3].background().style() == Qt.BrushStyle.NoBrush


def test_demand_badges_are_actually_painted_in_dark_theme(window, monkeypatch, qtbot):
    rows = [dict(id=i, name=str(i), basis='факт', demand_percent=value) for i, value in enumerate((0, 100), 1)]
    monkeypatch.setattr(app, 'market_ranking', lambda *args: {'rows': rows, 'preliminary': True})
    window.refresh_market()
    qtbot.waitUntil(lambda: window.market_table.rowCount() == 2)
    app.QApplication.processEvents()
    viewport = window.market_table.viewport()
    pixmap = viewport.grab()
    image = pixmap.toImage()
    scale = pixmap.devicePixelRatioF()
    colors = []
    for row in (0, 1):
        cell = window.market_table.visualRect(window.market_table.model().index(row, 1))
        colors.append(image.pixelColor(round((cell.left() + 10) * scale), round(cell.center().y() * scale)))
    assert colors[0].blue() > colors[0].red()
    assert colors[1].red() > colors[1].blue()


def test_market_sources_button_opens_its_product_without_selecting_another(window, qtbot, monkeypatch):
    product = window.store.ensure_product('Картон')
    monkeypatch.setattr(app, 'market_ranking', lambda *args: {'rows': [dict(id=product['id'], name='Картон', basis='Продажи: 100 шт. · 2026-11 · Россия', count=2, source_ids=[1, 2])], 'preliminary': False})
    calls = []
    def evidence(store, product_id, countries):
        calls.append((product_id, list(countries)))
        return [{'id': 1}, {'id': 2}]
    monkeypatch.setattr(app, 'market_evidence', evidence)
    shown = []
    class SourcePreview:
        def __init__(self, sources, parent):
            shown.extend(source['id'] for source in sources)
        def exec(self):
            return 0
    monkeypatch.setattr(app, 'SourcesDialog', SourcePreview)
    window.refresh_market()
    qtbot.waitUntil(lambda: window.market_table.rowCount() == 1)
    assert [window.market_table.horizontalHeaderItem(i).text() for i in range(3)] == ['Товар', 'Спрос', 'Источники']
    control = window.market_table.cellWidget(0, 2)
    assert control.text() == 'Источники · 2'
    qtbot.mouseClick(control, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: shown == [1, 2])
    assert shown == [1, 2]
    assert calls == [(product['id'], window.countries)]
    assert window.tabs.currentIndex() == 0
    assert window.product_id is None


def test_market_enter_opens_selected_branch_and_source_dialog_selects_real_fact(window, qtbot, monkeypatch):
    product = window.store.ensure_product('Кофе')
    monkeypatch.setattr(app, 'market_ranking', lambda *args: {'rows': [dict(id=product['id'], name='Кофе', basis='Продажи: 100 шт. · 2026-11 · Россия', count=2)], 'preliminary': False})
    window.refresh_market()
    qtbot.waitUntil(lambda: not window.workers)
    window.market_table.setCurrentCell(0, 1)
    qtbot.keyClick(window.market_table, Qt.Key.Key_Return)
    assert window.product_id == product['id']
    assert window.tabs.currentIndex() == 1
    assert not window.workers

    source_rows = [dict(url='https://first.example/report', title='Первый отчёт', published_at='2026-11-01', excerpt='Продано 100 единиц', fact='Продажи: 100 шт.'),
                   dict(url='https://second.example/report', title='Второй отчёт', published_at='2026-11-02', excerpt='Второй сохранённый факт', fact='Спрос вырос')]
    opened = []
    monkeypatch.setattr(dialogs, 'open_site', opened.append)
    dialog = SourcesDialog(source_rows, window)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.table.rowCount() == 2
    assert dialog.table.item(1, 0).text() == 'second.example'
    assert dialog.table.item(1, 1).text() == '2026-11-02'
    dialog.table.setCurrentCell(1, 2)
    assert 'Второй сохранённый факт' in dialog.text.toPlainText()
    assert 'Продано 100 единиц' not in dialog.text.toPlainText()
    open_button = next(item for item in dialog.findChildren(app.QPushButton) if item.text() == 'Открыть источник')
    qtbot.mouseClick(open_button, Qt.MouseButton.LeftButton)
    assert opened == ['https://second.example/report']
    dialog.close()


def test_phone_only_and_manual_missing_contacts_remain_visible(window, qtbot):
    product_id = window.store.ensure_product('Картон')['id']
    company(window.store, product_id, name='Только телефон', email='')
    company(window.store, product_id, name='Ручная карточка', email='', phone='', manual=1)
    company(window.store, product_id, name='Без контактов', email='', phone='')
    window.open_product(product_id)
    assert {r['name'] for r in window.company_rows} == {'Только телефон', 'Ручная карточка', 'Без контактов'}
    for row in range(window.company_table.rowCount()):
        assert window.company_table.item(row, 3).text() == 'Нет email'
    index = next(i for i, row in enumerate(window.company_rows) if row['name'] == 'Только телефон')
    qtbot.mouseClick(window.company_table.cellWidget(index, 2), Qt.MouseButton.LeftButton)
    assert app.QApplication.clipboard().text() == '+79991234567'


def test_company_without_contact_is_visible_as_needs_review_not_fit(window):
    product_id = window.store.ensure_product('Картон')['id']
    company_id = window.store.execute('INSERT INTO companies(name,created_at) VALUES(?,?)',
                                      ('Новый кандидат', now()))
    window.store.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids,checked_at,potential) '
                         'VALUES(?,?,?,?,?,?,0)',
                         (product_id, company_id, 'INSUFFICIENT_EVIDENCE', 'Нужно проверить.', '[]', now()))
    window.open_product(product_id)
    index = next(i for i, row in enumerate(window.company_rows) if row['id'] == company_id)
    assert window.company_table.item(index, 3).text() == 'Нужна проверка'
    assert window.company_table.cellWidget(index, 3) is None
    window.company_filter.setCurrentText('Подходят')
    assert company_id not in {row['id'] for row in window.company_rows}


def test_table_controls_fit_full_labels_inside_cells_at_minimum_window(window, qtbot, monkeypatch):
    product = window.store.ensure_product('Офисные кресла')
    company(window.store, product['id'], name='Очень длинное название производственного предприятия', phone='+7 999 123-45-67')
    monkeypatch.setattr(app, 'market_ranking', lambda *args: {'rows': [dict(id=product['id'], name='Офисные кресла', basis='Продажи: 100 шт. · Россия', count=2)], 'preliminary': False})
    window.resize(1020, 680)
    window.refresh_market()
    qtbot.waitUntil(lambda: window.market_table.rowCount() == 1)
    app.QApplication.processEvents()
    def full_label_fits(table, column):
        control = table.cellWidget(0, column)
        option = QStyleOptionButton()
        control.initStyleOption(option)
        content = control.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, control)
        assert content.width() >= control.fontMetrics().horizontalAdvance(control.text())
        assert content.height() >= control.fontMetrics().height()
        cell = table.visualRect(table.model().index(0, column))
        assert control.geometry().left() >= cell.left()
        assert control.geometry().right() <= cell.right()
    full_label_fits(window.market_table, 2)
    window.open_product(product['id'])
    app.QApplication.processEvents()
    for column in (2, 3, 5):
        full_label_fits(window.company_table, column)
    old_offer = window.company_table.cellWidget(0, 3)
    window.refresh_companies()
    assert old_offer.isHidden()


def test_preview_preserves_human_edits_and_does_not_send(window, qtbot):
    product_id = window.store.ensure_product('Картон')['id']
    company_id = company(window.store, product_id)
    offer_id = offer(window.store, product_id, company_id)
    window.open_product(product_id)
    dialog = OfferDialog(window, company_id)
    qtbot.addWidget(dialog)
    assert dialog.body.toPlainText() == 'Проверенный человеком текст'
    assert not window.store.query('SELECT * FROM outbound_attempts')
    dialog.body.setPlainText('Новая редакция пользователя')
    assert dialog.save()
    assert window.store.one('SELECT body,approved FROM offers WHERE id=?', (offer_id,)) == {'body': 'Новая редакция пользователя', 'approved': 0}
    dialog.close()


def test_recipient_selector_prioritizes_roles_persists_choice_and_sends_snapshot(window, qtbot, monkeypatch):
    product_id = window.store.ensure_product('Картон')['id']
    company_id = company(window.store, product_id)
    offer_id = offer(window.store, product_id, company_id)
    for value in ('billing@buyer.ru', 'hr@buyer.ru', 'sales@buyer.ru', 'test@example.com'):
        window.store.execute("INSERT INTO company_contacts(company_id,kind,value) VALUES(?,'email',?)", (company_id, value))
    window.open_product(product_id)
    dialog = OfferDialog(window, company_id)
    qtbot.addWidget(dialog)
    assert dialog.recipient == 'sales@buyer.ru'
    assert dialog.recipient_choice.count() == 4
    assert 'Продажи' in dialog.recipient_choice.itemText(0)
    dialog.recipient_choice.setCurrentIndex(dialog.recipient_choice.findData('info@buyer.ru'))
    assert dialog.save()
    assert window.store.one('SELECT recipient FROM offers WHERE id=?', (offer_id,))['recipient'] == 'info@buyer.ru'
    dialog.close()
    dialog = OfferDialog(window, company_id)
    qtbot.addWidget(dialog)
    assert dialog.recipient == 'info@buyer.ru'
    window.store.execute('INSERT INTO mail_accounts(id,email,provider,imap_host,smtp_host,connected) VALUES(1,?,?,?,?,1)',
                         ('owner@gmail.com','gmail','imap.gmail.com','smtp.gmail.com'))
    window.store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                         (company_id, 'info@buyer.ru', 'Согласие', 'Документ', now()))
    monkeypatch.setattr(dialogs, 'confirm', lambda *args: True)
    sent = []
    def send(offer_id, **kwargs):
        sent.append((offer_id, kwargs))
        return 'Отправлено'
    monkeypatch.setattr(window.mail, 'send_offer', send, raising=False)
    dialog.send()
    qtbot.waitUntil(lambda: not window.workers, timeout=3000)
    assert sent[0][0] == offer_id
    assert sent[0][1]['recipient'] == 'info@buyer.ru'
    assert sent[0][1]['expected']['body'] == 'Проверенный человеком текст'
    assert sent[0][1]['expected']['sender'] == 'owner@gmail.com'
    dialog.close()


def test_paused_reason_survives_other_notices_and_window_restart(window, qtbot):
    import json
    message = 'Достигнут лимит 100 запросов. Найдено товаров: 2. Нажмите «Найти товары» для продолжения.'
    window.store.execute('INSERT INTO job_runs(kind,status,countries,reason,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                         ('market','paused',json.dumps(window.countries,ensure_ascii=False),'limit',message,now(),now()))
    window.update_resume_labels()
    assert window.market_research_notice.text().startswith('Работа прервана.')
    window.guide('Проверка другого подключения')
    window.refresh_market()
    assert window.market_research_notice.text().startswith('Работа прервана.')
    other = app.Window(store=window.store,secrets=window.secrets)
    other.timer.stop(); other.startup_mail_timer.stop()
    qtbot.addWidget(other)
    assert other.market_research_notice.text().startswith('Работа прервана.')
    assert not other.market_research_notice.isHidden()
    other.close()


def test_cancelled_confirmation_never_sends(window, qtbot, monkeypatch):
    product_id = window.store.ensure_product('Картон')['id']
    company_id = company(window.store, product_id)
    offer_id = offer(window.store, product_id, company_id)
    window.open_product(product_id)
    window.store.execute('INSERT INTO mail_accounts(id,email,provider,imap_host,imap_port,smtp_host,smtp_port,connected,updated_at) VALUES(1,?,?,?,?,?,?,1,?)', ('owner@gmail.com', 'gmail', 'imap.gmail.com', 993, 'smtp.gmail.com', 465, now()))
    window.store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)', (company_id, 'info@buyer.ru', 'Согласие', 'Подтверждённое согласие', now()))
    monkeypatch.setattr(dialogs, 'confirm', lambda *args: False)
    dialog = OfferDialog(window, company_id)
    qtbot.addWidget(dialog)
    dialog.send()
    assert window.store.one('SELECT approved FROM offers WHERE id=?', (offer_id,))['approved'] == 0
    assert not window.store.query('SELECT * FROM outbound_attempts')
    assert not window.workers
    dialog.close()


def test_mail_history_filters_product_and_shows_uncertain(window, qtbot):
    first = window.store.ensure_product('Древесина')['id']
    second = window.store.ensure_product('Картон')['id']
    company_id = company(window.store, first)
    offer_id = offer(window.store, first, company_id)
    window.store.execute('INSERT INTO outbound_attempts(offer_id,idempotency_key,message_id,recipient,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)', (offer_id, 'once', '<one@buyer.ru>', 'info@buyer.ru', 'uncertain', now(), now()))
    for product_id, mid, body in [(first, '<one@buyer.ru>', 'Текст первого товара'), (second, '<two@buyer.ru>', 'Другой товар')]:
        window.store.execute('INSERT INTO mail_messages(product_id,company_id,message_id,direction,sender,recipient,subject,body,sent_at) VALUES(?,?,?,?,?,?,?,?,?)', (product_id, company_id, mid, 'outgoing', 'me@buyer.ru', 'info@buyer.ru', 'Тема', body, now()))
    dialog = HistoryDialog(window.store, first, company_id, window)
    qtbot.addWidget(dialog)
    assert dialog.list.count() == 1
    assert 'Проверяется' in dialog.list.item(0).text()
    assert 'Текст первого товара' in dialog.text.toPlainText()
    assert 'Другой товар' not in dialog.text.toPlainText()
    dialog.close()


def test_worker_network_function_runs_outside_ui_and_callback_on_ui(window, qtbot):
    main_thread = threading.get_ident()
    observed = {}
    def task(emit):
        observed['worker'] = threading.get_ident()
        emit('Проверяем')
        return 'Готово'
    def callback(result):
        observed['callback'] = threading.get_ident()
    window.submit(task, callback)
    qtbot.waitUntil(lambda: not window.workers, timeout=3000)
    assert observed['worker'] != main_thread
    assert observed['callback'] == main_thread


def test_market_country_filter_and_stale_evidence(window, qtbot):
    from sales_manager.research.market import extract_observations
    for country, name, date in [('Россия', 'Картон', now()), ('Казахстан', 'Древесина', now()), ('Россия', 'Старая тенденция', '2020-01-01T00:00:00+00:00')]:
        product_id = window.store.ensure_product(name)['id']
        month = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'][int(date[5:7]) - 1]
        text = f'{name}: продажи в {country} за {month} {date[:4]} года составили 100 шт.'
        source_id = window.store.put_source('https://example.org/' + str(product_id), name, text, date)
        for item in extract_observations(name, text, country, date):
            window.store.execute('INSERT INTO market_observations(product_id,source_id,metric,value,unit,period,country,published_at,excerpt,measured,event_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                                 (product_id, source_id, item['metric'], item['value'], item['unit'], item['period'], item['country'], item['published_at'], item['excerpt'], int(item['measured']), item['event_key'], date))
    window.countries = ['Россия']
    window.refresh_market()
    qtbot.waitUntil(lambda: not window.workers)
    assert [row['name'] for row in window.market_rows] == ['Картон']


def test_optout_saved_globally_for_company_email(window, qtbot):
    product_id = window.store.ensure_product('Картон')['id']
    company_id = company(window.store, product_id)
    dialog = CompanyDialog(window.store, product_id, company_id, window)
    qtbot.addWidget(dialog)
    dialog.optout.setChecked(True)
    dialog.save()
    assert window.store.one('SELECT email FROM opt_out')['email'] == 'info@buyer.ru'


def test_restore_optout_requires_new_confirmed_consent_for_selected_email(window, qtbot, monkeypatch):
    from PySide6.QtWidgets import QDialog, QLineEdit, QCheckBox, QMessageBox
    product_id = window.store.ensure_product('Картон')['id']
    company_id = company(window.store, product_id)
    window.store.execute("INSERT INTO company_contacts(company_id,kind,value) VALUES(?,'email','other@buyer.ru')", (company_id,))
    for address in ('info@buyer.ru', 'other@buyer.ru'):
        window.store.execute('INSERT INTO opt_out(email,created_at) VALUES(?,?)', (address, now()))
    window.store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                         (company_id, 'info@buyer.ru', 'Старое согласие', 'Старое свидетельство', now()))
    dialog = CompanyDialog(window.store, product_id, company_id, window)
    qtbot.addWidget(dialog)
    dialog.email.setCurrentText('info@buyer.ru')
    monkeypatch.setattr(QMessageBox, 'information', lambda *args: None)
    monkeypatch.setattr(dialogs, 'confirm', lambda *args: True)
    monkeypatch.setattr(QDialog, 'exec', lambda self: QDialog.DialogCode.Accepted)
    dialog.restore_permission()
    assert len(window.store.query('SELECT * FROM opt_out')) == 2
    def new_consent(box):
        box.findChild(QLineEdit).setText('16 ноября: письменное согласие клиента')
        box.findChild(QCheckBox).setChecked(True)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog, 'exec', new_consent)
    dialog.restore_permission()
    assert not window.store.one("SELECT email FROM opt_out WHERE email='info@buyer.ru'")
    assert window.store.one("SELECT email FROM opt_out WHERE email='other@buyer.ru'")
    assert window.store.one("SELECT evidence FROM permissions WHERE basis='Новое согласие'")['evidence']
    assert 'info@buyer.ru' in window.store.one("SELECT details FROM audit_events WHERE event='mail.opt_out_removed'")['details']


def test_manual_usage_reconciliation_requires_confirmation(window, monkeypatch):
    from sales_manager.ai import AIService
    window.ai = AIService(window.store, window.secrets)
    window.store.set_setting('ai.unconfirmed_tokens', 400)
    window.store.set_setting('ai.usage_incomplete', True)
    monkeypatch.setattr(app.QInputDialog, 'getInt', lambda *args: (70, True))
    monkeypatch.setattr(app.QMessageBox, 'question', lambda *args: app.QMessageBox.StandardButton.No)
    window.reconcile_ai_usage()
    assert window.store.get_setting('ai.unconfirmed_tokens') == 400
    monkeypatch.setattr(app.QMessageBox, 'question', lambda *args: app.QMessageBox.StandardButton.Yes)
    window.reconcile_ai_usage()
    assert window.store.get_setting('ai.unconfirmed_tokens') == 0
    assert window.store.one("SELECT input_tokens FROM usage_log WHERE operation='manual_reconciliation'")['input_tokens'] == 70


def test_balance_and_unconfirmed_usage_are_distinct(window):
    # The fixture must create a usable visible application window, not just
    # a detached settings label.
    assert window.isVisible()
    assert window.tabs.count() == 3
    # The balance widget lives on the Settings tab, which is hidden by default.
    window.tabs.setCurrentIndex(2)
    app.QApplication.processEvents()
    assert window.ai_status.isVisible()
    window.store.set_setting('ai.checked_at', now())
    window.store.set_setting('ai.balance', {'balance': [{'usage': 'GigaChat', 'value': 12345}]})
    window.store.set_setting('ai.usage_incomplete', True)
    window.store.set_setting('ai.unconfirmed_tokens', 400)
    window.update_ai_status()
    status = window.ai_status.text()
    assert 'Остаток: 12345 токенов' in status
    assert 'Подтверждённый расход: 0 токенов' in status
    assert 'Неподтверждённый расход' in status
    assert '400 токенов' in status


def test_gmail_default_and_credentials_saved_only_to_secret_store(window):
    assert window.mail_provider.currentData() == 'gmail'
    assert window.mail_provider.itemText(0) == 'Gmail (Google)'
    window.mail_email.setText('owner@gmail.com')
    window.mail_password.setText('abcd efgh ijkl mnop')
    assert window.save_settings()
    row = window.store.one('SELECT * FROM mail_accounts WHERE id=1')
    assert (row['provider'], row['imap_host'], row['imap_port'], row['smtp_host'], row['smtp_port']) == ('gmail', 'imap.gmail.com', 993, 'smtp.gmail.com', 465)
    assert window.secrets.get('mail_password') == 'abcd efgh ijkl mnop'
    assert 'двухэтап' in window.mail_help.text().lower()
    assert 'OAuth' in window.mail_help_detail.text()


def test_saved_yandex_connection_not_replaced_by_gmail_default(window, qtbot):
    window.mail_provider.setCurrentIndex(window.mail_provider.findData('yandex'))
    window.mail_email.setText('owner@yandex.ru')
    window.mail_password.setText('app-password')
    window.save_settings()
    window.store.execute('UPDATE mail_accounts SET connected=1,smtp_port=587 WHERE id=1')
    other = app.Window(store=window.store, secrets=window.secrets)
    other.timer.stop()
    other.startup_mail_timer.stop()
    qtbot.addWidget(other)
    assert other.mail_provider.currentData() == 'yandex'
    assert other.store.one('SELECT connected FROM mail_accounts WHERE id=1')['connected'] == 1
    assert other.save_settings()
    assert other.store.one('SELECT connected,smtp_port FROM mail_accounts WHERE id=1') == {'connected': 1, 'smtp_port': 587}
    other.close()


def test_all_sources_disabled_guides_to_settings_without_empty_job(window, monkeypatch, qtbot):
    for item in window.source_enabled.values():
        item.setChecked(False)
    assert window.save_settings()
    monkeypatch.setattr(window.research, 'available_sources', lambda operation: [], raising=False)
    window.start_market()
    assert not window.workers
    assert not window.store.query('SELECT * FROM job_runs')
    assert 'Нет доступных источников поиска' in window.action_notice.text()
    assert 'настройках' in window.action_notice.text()
    assert not hasattr(window, 'action_settings')
    window.tabs.setCurrentIndex(2)
    assert window.tabs.tabText(window.tabs.currentIndex()) == 'Настройки'


def test_manual_offer_draft_without_giga_and_mail_explain_next_step(window, monkeypatch):
    from PySide6.QtWidgets import QDialog

    product_id = window.store.ensure_product('Картон')['id']
    company_id = company(window.store, product_id)
    window.open_product(product_id)
    opened = []
    def close_draft(dialog):
        opened.append(dialog)
        assert dialog.offer and dialog.subject.text() == '' and dialog.body.toPlainText() == ''
        assert 'вручную' in dialog.notice.text() and 'GigaChat' in dialog.notice.text()
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(app.OfferDialog, 'exec', close_draft)
    window.preview_offer(company_id)
    assert len(opened) == 1
    assert window.store.one('SELECT subject,body,approved FROM offers WHERE product_id=? AND company_id=?',
                            (product_id, company_id)) == {'subject': '', 'body': '', 'approved': 0}
    assert not window.workers
    window.refresh_mail_button.trigger()
    assert 'подключите почту' in window.action_notice.text()
    window.test_mail()
    assert 'email' in window.mail_status.text()
    assert not window.workers


def test_each_source_test_calls_selected_provider_outside_ui(window, qtbot, monkeypatch):
    calls = []
    gate = threading.Event()
    main_thread = threading.get_ident()
    def test_source(name):
        calls.append((name, threading.get_ident()))
        gate.wait(1)
        return 'GDELT: Работает'
    monkeypatch.setattr(window.research, 'test_provider', test_source, raising=False)
    window.tabs.setCurrentIndex(2)
    window.source_selector.setCurrentIndex(window.source_selector.findData('gdelt'))
    qtbot.mouseClick(window.source_checks['gdelt'], Qt.MouseButton.LeftButton)
    assert window.search_check_busy
    assert not window.source_checks['gdelt'].isEnabled()
    window.tabs.setCurrentIndex(1)
    assert window.tabs.currentIndex() == 1
    gate.set()
    qtbot.waitUntil(lambda: not window.workers, timeout=3000)
    assert calls == [('gdelt', calls[0][1])]
    assert calls[0][1] != main_thread
    assert window.source_checks['gdelt'].isEnabled()
    assert 'Работает' in window.action_notice.text()


def test_unavailable_keyed_source_does_not_offer_a_checkbox_activation(window):
    window.test_search('tavily')
    assert not window.workers
    assert 'Введите API-ключ' in window.action_notice.text()
    assert window.source_checks['tavily'].isEnabled()
    assert window.free_confirmed.isHidden()


def test_source_switch_keeps_unsaved_keys_and_uses_selected_check(window, qtbot):
    window.tabs.setCurrentIndex(2)
    window.source_selector.setCurrentIndex(window.source_selector.findData('tavily'))
    window.source_keys['tavily'].setText('test-key-not-a-real-account')
    window.source_enabled['tavily'].setChecked(True)
    assert window.source_stack.currentWidget().isAncestorOf(window.source_checks['tavily'])
    assert not window.source_checks['tavily'].isHidden()

    window.source_selector.setCurrentIndex(window.source_selector.findData('gdelt'))
    window.source_enabled['gdelt'].setChecked(False)
    assert window.source_stack.currentWidget().isAncestorOf(window.source_checks['gdelt'])
    assert not window.source_checks['tavily'].isVisibleTo(window.source_stack)

    window.source_selector.setCurrentIndex(window.source_selector.findData('tavily'))
    assert window.source_keys['tavily'].text() == 'test-key-not-a-real-account'
    assert window.source_enabled['tavily'].isChecked()
    assert not window.source_enabled['gdelt'].isChecked()
    assert window.save_settings()
    assert window.secrets.get('tavily_key') == 'test-key-not-a-real-account'
    assert not window.store.get_setting('search.gdelt_enabled')


def test_no_white_background_and_clear_control_borders(window):
    style = app.QApplication.instance().styleSheet()
    assert 'background: white' not in style
    assert 'border: 1px solid #606060' in style
    assert app.QApplication.instance().palette().color(app.QPalette.ColorRole.Base).name() == '#303030'
    assert app.QApplication.instance().palette().color(app.QPalette.ColorRole.ButtonText).name() == '#f0f0f0'
    assert 'QComboBox QAbstractItemView' in style
    assert window.tabs.widget(0).objectName() == 'marketPage'
    assert window.tabs.widget(1).objectName() == 'companyPage'


def test_known_company_url_works_when_search_sources_disabled(window, monkeypatch, qtbot):
    assert not window.manual_company.isEnabled()
    product_id = window.store.ensure_product('Картон')['id']
    window.open_product(product_id)
    assert window.manual_company.isEnabled()
    calls = []
    monkeypatch.setattr(window.research, 'available_sources', lambda operation: [], raising=False)
    def find(product_id, countries, emit, cancel, manual_url=None):
        calls.append(manual_url)
        return {'status': 'completed'}
    monkeypatch.setattr(window.research, 'find_companies', find, raising=False)
    monkeypatch.setattr(app.QInputDialog, 'exec', lambda self: 1)
    monkeypatch.setattr(app.QInputDialog, 'textValue', lambda self: 'https://example.org')
    window.manual_company.trigger()
    qtbot.waitUntil(lambda: not window.workers, timeout=3000)
    assert calls == ['https://example.org']
    assert window.company_start.isEnabled()


def test_search_control_shows_busy_card_and_blocks_duplicate_search(window, monkeypatch, qtbot):
    calls = []
    def research(countries, emit, cancel):
        calls.append(cancel)
        cancel.wait(2)
        return {'status': 'complete'}
    monkeypatch.setattr(window.research, 'available_sources', lambda operation: ['rss'], raising=False)
    monkeypatch.setattr(window.research, 'research_market', research, raising=False)
    assert window.market_pause.isHidden()
    assert window.company_pause.isHidden()
    qtbot.mouseClick(window.market_start, Qt.MouseButton.LeftButton)
    assert window.busy_overlay.isVisible()
    assert window.busy_overlay.cancel_button.isVisible()
    assert not window.market_start.isEnabled()
    assert window.market_pause.isHidden()
    window.start_market()
    qtbot.mouseClick(window.busy_overlay.cancel_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not window.workers, timeout=3000)
    assert len(calls) == 1 and calls[0].is_set()
    assert window.busy_overlay.isHidden()
    assert window.market_start.isVisible()
    assert window.market_pause.isHidden()
    assert window.company_pause.isHidden()


def test_candidates_have_separate_status_filter_and_no_offer_action(window, qtbot):
    product = window.store.ensure_product('Офисные кресла')['id']
    confirmed = company(window.store, product, name='Подтверждённая компания')
    candidate = company(window.store, product, name='Кандидат')
    window.store.execute("UPDATE company_fit SET status='INSUFFICIENT_EVIDENCE',potential=1,reason='Производитель продукции; упаковка используется в отгрузке.' WHERE company_id=?", (candidate,))
    window.open_product(product)
    assert [row['id'] for row in window.company_rows] == [confirmed, candidate]
    assert window.company_table.item(1, 3).text() == 'Нужна проверка'
    assert window.company_table.cellWidget(1, 3) is None
    window.company_filter.setCurrentText('Нужна проверка')
    assert [row['id'] for row in window.company_rows] == [candidate]
    window.company_filter.setCurrentText('Подходят')
    assert [row['id'] for row in window.company_rows] == [confirmed]


def test_downgraded_fit_removes_previous_send_button(window):
    product = window.store.ensure_product('Коробки')['id']
    target = company(window.store, product)
    window.open_product(product)
    assert window.company_table.cellWidget(0, 3) is not None
    window.store.execute("UPDATE company_fit SET status='INSUFFICIENT_EVIDENCE',potential=1 WHERE company_id=?", (target,))
    window.refresh_companies()
    assert window.company_table.cellWidget(0, 3) is None
    assert window.company_table.item(0, 3).text() == 'Нужна проверка'


def test_without_offer_filter_excludes_saved_unsent_draft(window):
    product = window.store.ensure_product('Коробки')['id']
    with_draft = company(window.store, product, name='С черновиком')
    without_draft = company(window.store, product, name='Без черновика')
    offer(window.store, product, with_draft)
    window.open_product(product)
    window.company_filter.setCurrentText('Без УТП')
    assert [row['id'] for row in window.company_rows] == [without_draft]
    window.company_filter.setCurrentText('Все')
    assert {row['id'] for row in window.company_rows} == {with_draft, without_draft}


def test_semantic_review_error_is_visible_inside_company_dialog(window, qtbot, monkeypatch):
    product = window.store.ensure_product('Коробки')['id']
    target = company(window.store, product)
    window.store.execute("UPDATE company_fit SET status='INSUFFICIENT_EVIDENCE',potential=1 WHERE company_id=?", (target,))
    window.secrets.set('gigachat_key', 'isolated-test-key')
    def assess(*args, **kwargs):
        raise ValueError('GigaChat недоступен. Повторите проверку.')
    monkeypatch.setattr(window.ai, 'assess_fit', assess, raising=False)
    window.open_product(product)
    dialog = CompanyDialog(window.store, product, target, window)
    qtbot.addWidget(dialog)
    dialog.show()
    assess_button = next(item for item in dialog.findChildren(app.QPushButton) if item.text() == 'Оценить применимость')
    details = dialog.findChild(dialogs.QPlainTextEdit)
    qtbot.mouseClick(assess_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not window.workers, timeout=3000)
    assert 'GigaChat недоступен' in details.toPlainText()
    assert 'Производство мебели' in details.toPlainText()
    assert 'оценивает сохранённые факты' not in details.toPlainText()
    assert assess_button.isEnabled()
    qtbot.mouseClick(assess_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not window.workers, timeout=3000)
    assert details.toPlainText().count('GigaChat недоступен') == 1
    assert window.store.one('SELECT status FROM company_fit WHERE company_id=?', (target,))['status'] == 'INSUFFICIENT_EVIDENCE'
    dialog.close()


def test_empty_offer_draft_can_be_created_and_saved_without_gigachat(window, qtbot):
    product_id = window.store.ensure_product('Картон')['id']
    company_id = company(window.store, product_id)
    window.open_product(product_id)
    window.secrets.values.clear()
    dialog = OfferDialog(window, company_id)
    qtbot.addWidget(dialog)
    assert dialog.offer and dialog.subject.text() == '' and dialog.body.toPlainText() == ''
    dialog.subject.setText('Черновая тема')
    assert dialog.save()
    saved = window.store.one('SELECT subject,body,approved FROM offers WHERE product_id=? AND company_id=?',
                             (product_id, company_id))
    assert saved == {'subject': 'Черновая тема', 'body': '', 'approved': 0}
    dialog.close()


def test_manual_contact_is_saved_with_user_provided_source(window, qtbot, monkeypatch):
    product_id = window.store.ensure_product('Картон')['id']
    company_id = company(window.store, product_id)
    window.open_product(product_id)
    class Field:
        def __init__(self, value):
            self._value = value
        def text(self):
            return self._value
    class Kind:
        def currentData(self):
            return 'email'
    class Entry:
        def __init__(self, *args):
            self.value, self.source, self.kind = Field('buying@buyer.ru'), Field('https://buyer.example/contacts'), Kind()
        def exec(self):
            return dialogs.QDialog.DialogCode.Accepted
    monkeypatch.setattr(dialogs, 'ManualContactDialog', Entry)
    dialog = CompanyDialog(window.store, product_id, company_id, window)
    qtbot.addWidget(dialog)
    dialog.add_manual_contact()
    contact = window.store.one("SELECT value,source_id FROM company_contacts WHERE company_id=? AND value='buying@buyer.ru'", (company_id,))
    assert contact and dialog.email.currentText() == 'buying@buyer.ru'
    source = window.store.one('SELECT url,title,text FROM sources WHERE id=?', (contact['source_id'],))
    assert source['url'] == 'https://buyer.example/contacts'
    assert source['title'] == 'Ручной источник контакта' and 'указанная пользователем' in source['text']
    assert not window.store.one("SELECT id FROM permissions WHERE company_id=? AND email='buying@buyer.ru'", (company_id,))
    dialog.close()


def test_rejected_submit_releases_send_dialog_lock(window, qtbot, monkeypatch):
    product_id = window.store.ensure_product('Картон')['id']
    company_id = company(window.store, product_id)
    offer_id = offer(window.store, product_id, company_id)
    window.open_product(product_id)
    window.store.execute('INSERT INTO mail_accounts(id,email,provider,imap_host,smtp_host,connected) VALUES(1,?,?,?,?,1)',
                         ('owner@gmail.com','gmail','imap.gmail.com','smtp.gmail.com'))
    window.store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                         (company_id, 'info@buyer.ru', 'Согласие', 'Подтверждение', now()))
    monkeypatch.setattr(dialogs, 'confirm', lambda *args: True)
    monkeypatch.setattr(window, 'submit', lambda *args, **kwargs: None)
    dialog = OfferDialog(window, company_id)
    qtbot.addWidget(dialog)
    dialog.send()
    assert not window.mail_busy
    assert dialog.send_button.isEnabled()
    assert 'Не удалось начать отправку' in dialog.notice.text()
    assert window.store.one('SELECT approved FROM offers WHERE id=?', (offer_id,))['approved'] == 1
    dialog.close()


def test_market_publication_caption_does_not_claim_sales(window, qtbot):
    from sales_manager.research.market import extract_observations
    product = window.store.ensure_product('Кофе')['id']
    date = now()
    month = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'][int(date[5:7]) - 1]
    text = f'В России за {month} {date[:4]} года спрос на кофе вырос.'
    source = window.store.put_source('https://example.org/retail-news', 'Спрос на кофе вырос', text, date)
    for item in extract_observations('кофе', text, 'Россия', date):
        window.store.execute('INSERT INTO market_observations(product_id,source_id,metric,value,unit,period,country,published_at,excerpt,measured,event_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                             (product, source, item['metric'], item['value'], item['unit'], item['period'], item['country'], item['published_at'], item['excerpt'], int(item['measured']), item['event_key'], date))
    window.countries = ['Россия']
    window.refresh_market()
    qtbot.waitUntil(lambda: not window.workers)
    assert window.market_rows[0]['id'] == product
    assert 'По данным публикаций' in window.market_hint.text()
    assert 'с 2025 года по настоящее время' in window.market_hint.text()
    assert 'Последняя публикация ' + '.'.join(reversed(date[:10].split('-'))) in window.market_hint.text()
    assert not window.market_rows[0]['measured']


def test_close_cancels_delayed_startup_mail_check(window, monkeypatch, qtbot):
    calls = []
    window.store.execute('INSERT INTO mail_accounts(id,email,provider,imap_host,imap_port,smtp_host,smtp_port,connected,updated_at) VALUES(1,?,?,?,?,?,?,1,?)', ('owner@gmail.com', 'gmail', 'imap.gmail.com', 993, 'smtp.gmail.com', 465, now()))
    monkeypatch.setattr(window.mail, 'sync', lambda: calls.append('sync'), raising=False)
    window.startup_mail_timer.start(10)
    window.close()
    qtbot.wait(30)
    assert not calls
    assert not window.workers
    assert not window.startup_mail_timer.isActive()


def test_startup_mail_waits_for_foreground_maintenance(window, monkeypatch, qtbot):
    gate = threading.Event()
    calls = []
    monkeypatch.setattr(window, 'sync_mail', lambda: calls.append('sync'))
    window.submit(lambda emit: gate.wait(3), busy_message='Обслуживаем базу данных')
    try:
        window.sync_startup_mail()
        assert window.startup_mail_timer.isActive()
        assert calls == []
    finally:
        gate.set()
    qtbot.waitUntil(lambda: calls == ['sync'], timeout=2500)


def test_close_cancels_active_mail_worker(window, monkeypatch, qtbot):
    window.store.execute("INSERT INTO mail_accounts(id,email,connected) VALUES(1,'owner@example.org',1)")
    started = threading.Event()
    socket_cancellation = []

    def sync(cancel):
        assert cancel is window.mail_cancel
        started.set()
        cancel.wait(5)
        return 'Почта обновлена'

    monkeypatch.setattr(window.mail, 'sync', sync, raising=False)
    monkeypatch.setattr(window.mail, 'cancel_network',
                        lambda: socket_cancellation.append(window.mail_cancel.is_set()), raising=False)
    window.sync_mail(manual=True)
    qtbot.waitUntil(started.is_set)
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=2000)
    assert socket_cancellation == [True]
    assert not window.workers


def test_sent_folder_picker_persists_selected_wire_name(window, monkeypatch, qtbot):
    window.store.execute("INSERT INTO mail_accounts(id,email,connected,sent_folder) VALUES(1,'owner@example.org',1,'Sent')")
    folders = [('Автоматически', ''), ('Отправленные', 'Sent'), ('Автоматически', 'Custom')]
    monkeypatch.setattr(window.mail, 'list_folders', lambda cancel: folders, raising=False)
    monkeypatch.setattr(window.mail, 'set_sent_folder',
                        lambda wire: window.store.execute('UPDATE mail_accounts SET sent_folder=?', (wire,)), raising=False)

    def choose(_parent, _title, _label, choices, current, editable):
        assert current == 1 and not editable
        assert len(set(choices)) == len(choices)
        return choices[-1], True

    monkeypatch.setattr(app.QInputDialog, 'getItem', choose)
    window.choose_sent_folder()
    qtbot.waitUntil(lambda: not window.workers)
    assert window.store.one('SELECT sent_folder FROM mail_accounts')['sent_folder'] == 'Custom'


def test_offer_numeric_warning_is_visible_and_rechecked_after_edit(window, qtbot):
    from sales_manager.ai import AIService

    pid = window.store.ensure_product('Картон')['id']
    cid = company(window.store, pid)
    row = window.store.one('SELECT source_ids FROM company_fit WHERE company_id=?', (cid,))
    oid = offer(window.store, pid, cid, 'Выручка выросла до 3 млн рублей <b>за квартал</b>.')
    window.store.execute("UPDATE offers SET cache_key='generated',source_ids=? WHERE id=?", (row['source_ids'], oid))
    window.ai = AIService(window.store, window.secrets)
    window.open_product(pid)
    dialog = OfferDialog(window, cid)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.numeric_notice.isVisible()
    assert '3 млн рублей' in dialog.numeric_notice.text()
    assert '&lt;b&gt;' in dialog.numeric_notice.toolTip()
    dialog.body.setPlainText('Предлагаем созвониться через 3 дня.')
    qtbot.waitUntil(dialog.numeric_notice.isHidden)
    dialog.close()


def test_invalid_rss_url_never_partially_saves_other_settings(window, monkeypatch):
    shown = []
    monkeypatch.setattr(app.QMessageBox, 'information', lambda *args: shown.append(str(args[-1])))
    old_name = window.store.get_setting('sender.name', 'Аргон')
    old_secret = window.secrets.get('gigachat_key')
    window.sender_name.setText('Изменённая компания')
    window.giga_key.setText('new-secret')
    window.extra_source_fields['rss_urls'].setText('not-an-http-url')
    assert not window.save_settings()
    assert shown and 'URL' in shown[-1]
    assert window.store.get_setting('sender.name', 'Аргон') == old_name
    assert window.secrets.get('gigachat_key') == old_secret


def test_mail_busy_rejection_keeps_every_setting_unchanged(window, monkeypatch):
    monkeypatch.setattr(app.QMessageBox, 'information', lambda *args: None)
    old_name = window.store.get_setting('sender.name', 'Аргон')
    old_secret = window.secrets.get('gigachat_key')
    window.sender_name.setText('Новое имя')
    window.giga_key.setText('modified-giga-key')
    window.mail_email.setText('new@gmail.com')
    window.mail_password.setText('new-app-password')
    window.mail_busy = True
    assert not window.save_settings()
    assert window.store.get_setting('sender.name', 'Аргон') == old_name
    assert window.secrets.get('gigachat_key') == old_secret
    assert window.store.one('SELECT * FROM mail_accounts WHERE id=1') is None


def test_settings_credential_failure_is_visible_and_retains_connected_mailbox(window, monkeypatch):
    window.sender_name.setText('Сохранённое имя')
    window.giga_key.setText('old-giga')
    window.mail_email.setText('owner@gmail.com')
    window.mail_password.setText('old-password')
    assert window.save_settings()
    window.store.execute('UPDATE mail_accounts SET connected=1,last_uid=42 WHERE id=1')
    original_set = window.secrets.set
    fail_once = [True]
    def faulty_set(key, value):
        original_set(key, value)
        if key == 'tavily_key' and fail_once[0]:
            fail_once[0] = False
            raise RuntimeError('Credential Manager недоступен')
    monkeypatch.setattr(window.secrets, 'set', faulty_set)
    shown = []
    monkeypatch.setattr(app.QMessageBox, 'information', lambda *args: shown.append(str(args[-1])))
    window.sender_name.setText('Несохранённое имя')
    window.giga_key.setText('new-giga')
    window.source_keys['tavily'].setText('new-tavily')
    window.mail_password.setText('new-password')
    assert not window.save_settings()
    assert shown == ['Credential Manager недоступен']
    assert window.store.get_setting('sender.name') == 'Сохранённое имя'
    assert window.store.one('SELECT connected,last_uid FROM mail_accounts') == {'connected': 1, 'last_uid': 42}
    assert window.secrets.get('gigachat_key') == 'old-giga'
    assert window.secrets.get('tavily_key') == ''
    assert window.secrets.get('mail_password') == 'old-password'


def test_async_mail_error_is_visible_and_connect_button_becomes_reusable(window, qtbot, monkeypatch):
    window.mail_email.setText('owner@gmail.com')
    window.mail_password.setText('previous-password')
    assert window.save_settings()
    window.store.execute("UPDATE mail_accounts SET connected=1,last_uid=72,uidvalidity='8',sent_folder='Sent'")
    previous = window.store.one('SELECT * FROM mail_accounts')
    window.mail_password.setText('isolated-app-password')
    gate = threading.Event()
    from sales_manager.mail.service import MailAuthenticationError

    def failed_connection(values, password, cancel):
        assert values[0] == 'owner@gmail.com' and password == 'isolated-app-password'
        gate.wait(1)
        raise MailAuthenticationError('Gmail: неверный пароль приложения. Проверьте настройки.')
    monkeypatch.setattr(window.mail, 'verify_connection', failed_connection, raising=False)
    window.test_mail()
    assert window.mail_busy
    assert not window.mail_test.isEnabled()
    window.tabs.setCurrentIndex(1)
    assert window.tabs.currentIndex() == 1
    gate.set()
    qtbot.waitUntil(lambda: not window.workers, timeout=3000)
    assert 'неверный пароль приложения' in window.mail_status.text()
    assert window.action_notice.text() == window.mail_status.text()
    assert window.mail_test.isEnabled()
    assert not window.mail_busy
    assert window.store.one('SELECT * FROM mail_accounts') == previous
    assert window.secrets.get('mail_password') == 'previous-password'

    monkeypatch.setattr(window.mail, 'verify_connection', lambda *_: 'Sent Mail', raising=False)
    window.test_mail()
    qtbot.waitUntil(lambda: not window.workers, timeout=3000)
    assert window.mail_status.text() == 'Подключено'
    assert window.secrets.get('mail_password') == 'isolated-app-password'
    assert window.store.one('SELECT connected,last_uid,uidvalidity,sent_folder FROM mail_accounts') == {
        'connected': 1, 'last_uid': 72, 'uidvalidity': '8', 'sent_folder': 'Sent Mail'}
