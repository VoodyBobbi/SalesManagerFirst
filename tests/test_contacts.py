"""Recipient purpose and exclusion of placeholder addresses."""
import pytest
from sales_manager.core.contacts import is_contact_email, email_purpose
from sales_manager.research.extraction import emails_in, parse_page


@pytest.mark.parametrize('value', ['sales@example.com','info@sub.example.org','a@localhost.test',
                                  'test@firm.ru','test123@firm.ru','dummy-sales@firm.ru',
                                  'person@site.invalid',
                                  'name@domain.com','email@domain.com','your@email.com'])
def test_placeholder_is_not_a_real_company_contact(value):
    assert not is_contact_email(value)
    assert emails_in(value) == []


@pytest.mark.parametrize('email,purpose', [('sales@firm.ru','Продажи'),('zakupki@firm.ru','Закупки'),
    ('purchase@firm.ru','Закупки'),('purchasing@firm.ru','Закупки'),('info@firm.ru','Общий'),
    ('hr@firm.ru','Кадры'),('buh@firm.ru','Бухгалтерия'),('buhgalter@firm.ru','Бухгалтерия'),('accounting@firm.ru','Бухгалтерия')])
def test_email_purpose_uses_address_not_insert_order(email,purpose):
    assert is_contact_email(email)
    assert email_purpose(email)[1] == purpose


@pytest.mark.parametrize('email', ['.sales@firm.ru', 'sales.@firm.ru', 'sales..team@firm.ru',
                                  'sales@firm..ru', 'sales@firm-.ru', 'sales@sub.-firm.ru',
                                  'x' * 65 + '@firm.ru'])
def test_malformed_syntax_is_not_suggested_as_a_contact(email):
    assert not is_contact_email(email)


@pytest.mark.parametrize('email', ['name@trustedcompany.ru', 'email@actualbusiness.ru',
                                   'your@actualbusiness.ru', 'sales@firm.ru', 'user@firm.ru', 'user.name@firm.ru'])
def test_real_company_domains_are_not_rejected_by_placeholder_filter(email):
    assert is_contact_email(email)
    assert emails_in('Для связи: ' + email) == [email]


def test_template_addresses_are_excluded_from_parsed_company_site():
    from sales_manager.research.extraction import parse_page
    page = parse_page("""<html><head><title>Магазин</title></head><body>
            <a href="mailto:name@domain.com">Наша почта</a>
            <a href="mailto:email@domain.com">Шаблон</a>
            <a href="mailto:your@email.com">Шаблон</a>
            <a href="mailto:sales@firm.ru">Отдел продаж</a>
            </body></html>""", 'https://firm.ru/contact')
    assert page['emails'] == ['sales@firm.ru']


def test_previously_saved_template_contacts_are_not_suggested(tmp_path):
    from sales_manager.core.contacts import company_emails
    from sales_manager.storage import Store
    store = Store(tmp_path / 'contacts.sqlite3')
    try:
        company_id = store.execute(
            'INSERT INTO companies(name,domain,url,country,created_at) VALUES(?,?,?,?,?)',
            ('Магазин', 'firm.ru', 'https://firm.ru', 'Россия', '2026-11-14T00:00:00+00:00'))
        for email in ('name@domain.com', 'email@domain.com', 'your@email.com', 'sales@firm.ru'):
            store.execute(
                "INSERT INTO company_contacts(company_id,kind,value) VALUES(?,'email',?)",
                (company_id, email))
        assert company_emails(store, company_id) == [
            {'email': 'sales@firm.ru', 'purpose': 'Продажи'}]
    finally:
        store.close()


def test_idna_email_is_extracted_from_text_and_mailto():
    page = parse_page('<p>Продажи: sales@фирма.рф.</p><a href="mailto:info@фирма.рф">Email</a>',
                      'https://фирма.рф/contact', 'Россия')
    assert page['emails'] == ['info@xn--80apmps.xn--p1ai', 'sales@xn--80apmps.xn--p1ai']
    assert is_contact_email('sales@xn--80adxhks.xn--p1ai')
    assert not is_contact_email('sales@-фирма.рф')
    assert not is_contact_email('sales@фирма.xn--')


def test_russian_phones_without_prefix_preserve_context():
    page = parse_page('<p>(495) 123-45-67</p><p>343-456-78-90</p><p>Тел. 9612345678</p>'
                      '<p>ИНН 495 675 32 17</p><p>Артикул 9991234567</p>'
                      '<p>Платёжный оператор: (812) 345-67-89</p>',
                      'https://firm.ru/contact', 'Россия')
    assert page['phones'] == ['+73434567890', '+74951234567', '+79612345678']
