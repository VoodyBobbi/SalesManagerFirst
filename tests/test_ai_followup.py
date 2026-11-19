"""Regression cases for calendar abbreviations, complete evidence and manual FIT."""
import json
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QWidget

from sales_manager.ai.service import AIService, _NUMBER, _quantity_qualifier
from sales_manager.storage import Store, now
from sales_manager.ui.dialogs import CompanyDialog


@pytest.mark.parametrize('source,body', [
    ('Работаем с 2015 г.', 'Работаем с 2015 года.'),
    ('Отчёт за 2026 г.', 'Отчёт за 2026 год.'),
    ('В ноябре 2026 г. опубликован отчёт.', 'В ноябре 2026 года опубликован отчёт.'),
    ('Работаем с 2015 года.', 'Работаем с 2015 г.'),
])
def test_calendar_abbreviations_allow_grounded_draft(source, body):
    card = {'product': 'Товар', 'company': 'Покупатель', 'sender': 'Аргон',
            'sources': [{'id': 1, 'url': 'https://buyer.test/', 'text': source}]}
    draft = {'subject': 'Сотрудничество', 'body': body,
             'reason': 'Гипотеза: сотрудничество.', 'source_ids': [1]}
    assert AIService.validate_offer(draft, card)['body'] == body


@pytest.mark.parametrize('text', ['Масса 500 г.', 'Масса 2015 г.', 'Объём партии 2026 г. сахара.'])
def test_four_digit_weights_remain_grams(text):
    match = next(_NUMBER.finditer(text))
    assert _quantity_qualifier(text, match.end()) == ('', 'g')


def test_grams_cannot_confirm_a_calendar_year():
    card = {'sources': [{'id': 1, 'url': 'https://buyer.test/', 'text': 'Масса 2015 г.'}]}
    draft = {'subject': 'Предложение', 'body': 'Работаем с 2015 года.',
             'reason': 'Гипотеза: сотрудничество.', 'source_ids': [1]}
    with pytest.raises(ValueError, match='неподтвержд'):
        AIService.validate_offer(draft, card)


class Keys:
    def get(self, key, default=''):
        return 'test-key' if key == 'gigachat_key' else default


@pytest.fixture
def buyer(tmp_path):
    store = Store(tmp_path / 'buyer.sqlite3')
    pid = store.ensure_product('Принтер')['id']
    cid = store.execute('INSERT INTO companies(name,url,created_at) VALUES(?,?,?)',
                        ('Типография', 'https://buyer.test/', now()))
    quote = 'Мы печатаем книги и учебники для школ.'
    sid = store.put_source('https://buyer.test/about', text=quote)
    extra = store.put_source('https://www.openstreetmap.org/node/123', text='name: Типография; office: company')
    store.execute('INSERT INTO company_fit(product_id,company_id,status,reason,source_ids) VALUES(?,?,?,?,?)',
                  (pid, cid, 'INSUFFICIENT_EVIDENCE', 'Нужна проверка.', json.dumps([sid, extra])))
    result = {'status': 'FIT', 'reason': 'Гипотеза: принтеры могут использоваться при печати книг.',
              'source_ids': [sid], 'evidence_quote': quote}
    ai = AIService(store, Keys())
    calls = []

    def completion(*args):
        calls.append(args)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}]}

    ai._completion = completion
    yield store, ai, pid, cid, sid, extra, quote, result, calls
    store.close()


def test_semantic_review_keeps_all_sources_and_cache_reuses_result(buyer):
    store, ai, pid, cid, sid, extra, quote, result, calls = buyer
    for _ in range(2):
        assert ai.assess_fit(pid, cid)['status'] == 'FIT'
        assert json.loads(store.one('SELECT source_ids FROM company_fit')['source_ids']) == [sid, extra]
    assert len(calls) == 1
    assert store.one('SELECT COUNT(*) n FROM sources')['n'] == 2


@pytest.mark.parametrize('refusal', ['Мы не закупаем принтеры.', 'Компания ликвидирована.'])
@pytest.mark.parametrize('location', ['after_excerpt', 'fifth_source'])
def test_full_evidence_blocks_hidden_refusals_without_expanding_prompt(buyer, refusal, location):
    store, ai, pid, cid, sid, extra, quote, result, calls = buyer
    if location == 'after_excerpt':
        full = quote + (' Описание работы.' * 130) + ' ' + refusal
        sid = store.put_source('https://buyer.test/about', text=full)
        ids = [sid, extra]
        result['source_ids'] = [sid]
    else:
        ids = [sid, extra]
        ids.extend(store.put_source('https://buyer.test/page/' + str(i), text='Описание деятельности компании.') for i in range(2))
        ids.append(store.put_source('https://buyer.test/conditions', text=refusal))
    store.execute('UPDATE company_fit SET source_ids=?', (json.dumps(ids),))
    assert ai.assess_fit(pid, cid)['status'] == 'INSUFFICIENT_EVIDENCE'
    prompt = json.loads(calls[0][2])
    assert len(prompt['sources']) <= 4
    assert all(len(source['text']) <= 1200 for source in prompt['sources'])
    assert json.loads(store.one('SELECT source_ids FROM company_fit')['source_ids']) == ids


def test_changed_full_evidence_cannot_reuse_old_positive_cache(buyer):
    store, ai, pid, cid, sid, extra, quote, result, calls = buyer
    ids = [sid, extra]
    ids.extend(store.put_source('https://buyer.test/page/' + str(i), text='Описание деятельности компании.') for i in range(2))
    fifth = store.put_source('https://buyer.test/conditions', text='Условия сотрудничества с типографией.')
    ids.append(fifth)
    store.execute('UPDATE company_fit SET source_ids=?', (json.dumps(ids),))
    before = ai._card(pid, cid, require_fit=False)['sources']
    assert ai.assess_fit(pid, cid)['status'] == 'FIT'
    changed = store.put_source('https://buyer.test/conditions', text='Мы не закупаем принтеры.')
    ids[-1] = changed
    store.execute('UPDATE company_fit SET source_ids=?', (json.dumps(ids),))
    assert ai._card(pid, cid, require_fit=False)['sources'] == before
    assert ai.assess_fit(pid, cid)['status'] == 'INSUFFICIENT_EVIDENCE'
    assert len(calls) == 2


@pytest.mark.parametrize('status', ['FIT', 'NOT_FIT', 'INSUFFICIENT_EVIDENCE'])
@pytest.mark.parametrize('cached', [True, False])
def test_automatic_and_cached_ai_never_replace_manual_decision(buyer, status, cached):
    store, ai, pid, cid, sid, extra, quote, result, calls = buyer
    if cached:
        ai.assess_fit(pid, cid)
    store.set_company_fit_manual(pid, cid, status, 'Проверено лично пользователем.')
    previous = store.one('SELECT * FROM company_fit')
    call_count = len(calls)
    for force in (False, True):
        with pytest.raises(ValueError, match='ручное решение'):
            ai.assess_fit(pid, cid, force=force)
    assert store.one('SELECT * FROM company_fit') == previous
    assert len(calls) == call_count


def test_company_card_disables_ai_after_manual_save(buyer, qtbot):
    store, ai, pid, cid, sid, extra, quote, result, calls = buyer

    class Parent(QWidget):
        def __init__(self):
            super().__init__()
            self.ai, self.secrets, self.submitted = ai, Keys(), []

        def submit(self, *args, **kwargs):
            self.submitted.append((args, kwargs))

        def refresh_companies(self):
            pass

    parent = Parent()
    qtbot.addWidget(parent)
    dialog = CompanyDialog(store, pid, cid, parent)
    qtbot.addWidget(dialog)
    assert dialog.fit_assess is not None and dialog.fit_assess.isEnabled()
    dialog.fit_status.setCurrentIndex(dialog.fit_status.findData('NOT_FIT'))
    dialog.fit_reason.setText('Проверено: предложение компании не подходит.')
    dialog.save_fit()
    assert not dialog.fit_assess.isEnabled()
    dialog.fit_assess.click()
    assert parent.submitted == []
    reopened = CompanyDialog(store, pid, cid, parent)
    qtbot.addWidget(reopened)
    assert reopened.fit_assess is None


def test_company_card_keeps_manual_draft_after_automated_fit(buyer, qtbot):
    store, ai, pid, cid, sid, extra, quote, result, calls = buyer
    parent = QWidget()
    parent.ai, parent.secrets = ai, Keys()
    parent.refresh_companies = lambda: None

    def submit(task, done, *, finished, **options):
        done(task(lambda message: None))
        finished()

    parent.submit = submit
    qtbot.addWidget(parent)
    dialog = CompanyDialog(store, pid, cid, parent)
    qtbot.addWidget(dialog)
    dialog.fit_status.setCurrentIndex(dialog.fit_status.findData('NOT_FIT'))
    dialog.fit_reason.setText('Компания прекратила закупки.')
    dialog.fit_assess.click()

    assert store.one('SELECT status FROM company_fit')['status'] == 'FIT'
    assert dialog.fit_status.currentData() == 'NOT_FIT'
    assert dialog.fit_reason.text() == 'Компания прекратила закупки.'
    dialog.fit_save.click()
    assert store.one('SELECT status,reason FROM company_fit') == {
        'status': 'NOT_FIT', 'reason': 'Компания прекратила закупки.'}


def test_open_card_rechecks_manual_state_before_starting_ai(buyer, qtbot):
    store, ai, pid, cid, sid, extra, quote, result, calls = buyer
    parent = QWidget()
    parent.ai, parent.secrets = ai, Keys()
    parent.submit = lambda *args, **kwargs: pytest.fail('Manual decision must not invoke AI')
    qtbot.addWidget(parent)
    dialog = CompanyDialog(store, pid, cid, parent)
    qtbot.addWidget(dialog)
    store.set_company_fit_manual(pid, cid, 'FIT', 'Подтверждено в другой открытой карточке.')
    dialog.fit_assess.click()
    assert not dialog.fit_assess.isEnabled()
