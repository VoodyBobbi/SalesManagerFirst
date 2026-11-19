"""Unknown units must not become equivalent simply because they are unknown."""
import pytest

from sales_manager.ai.service import AIService


@pytest.mark.parametrize(('source_unit', 'draft_unit'), [
    ('дней', 'сотрудников'), ('зданий', 'компаний'), ('больных', 'рабочих'),
])
def test_unknown_quantity_units_do_not_match_even_in_same_context(source_unit, draft_unit):
    card = {'product': 'Техника', 'company': 'Компания', 'sender': 'Аргон', 'signature': '',
            'sources': [{'id': 1, 'url': 'https://example.test',
                         'text': f'Производство составляет 100 {source_unit} в году.'}]}
    draft = {'subject': 'Предложение', 'body': f'Производство составляет 100 {draft_unit} в году.',
             'reason': 'Обсудим применение.', 'source_ids': [1]}
    with pytest.raises(ValueError, match='неподтвержд'):
        AIService.validate_offer(draft, card)


@pytest.mark.parametrize('other', ['параметров', 'параграфов'])
def test_words_starting_with_para_cannot_confirm_pairs(other):
    card = {'product': 'Обувь', 'company': 'Компания', 'sender': 'Аргон', 'signature': '',
            'sources': [{'id': 1, 'url': 'https://example.test',
                         'text': f'Компания проверяет 10 {other} обуви.'}]}
    draft = {'subject': 'Предложение', 'body': 'Компания проверяет 10 пар обуви.',
             'reason': 'Обсудим применение.', 'source_ids': [1]}
    with pytest.raises(ValueError, match='неподтвержд'):
        AIService.validate_offer(draft, card)

    card['sources'][0]['text'] = draft['body']
    assert AIService.validate_offer(draft, card)['body'] == draft['body']
