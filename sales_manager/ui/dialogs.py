"""Company evidence, mail preview and history, always rendered as plain text."""
import json
from urllib.parse import urlsplit

from PySide6.QtCore import QSignalBlocker, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from shiboken6 import isValid
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QMessageBox,
    QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from sales_manager.storage import now
from sales_manager.core.contacts import company_emails, email_purpose, is_contact_email
from sales_manager.core.offer_fields import MAX_SUBJECT_LENGTH
from sales_manager.mail.service import save_offer
from .theme import enable_row_hover, notice_tone


def label(text):
    item = QLabel(text)
    item.setTextFormat(Qt.TextFormat.PlainText)
    item.setWordWrap(True)
    return item


def open_site(url):
    parsed = urlsplit(url)
    if parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username:
        QDesktopServices.openUrl(QUrl(url))


def confirm(parent, title, text, action='Продолжить'):
    box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=parent)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.button(QMessageBox.StandardButton.Yes).setText(action)
    box.button(QMessageBox.StandardButton.No).setText('Отмена')
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


class CountriesDialog(QDialog):
    def __init__(self, countries, selected, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Страны')
        self.setMinimumWidth(300)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        self.checks = []
        for name in countries:
            checkbox = QCheckBox(name)
            checkbox.setChecked(name in selected)
            layout.addWidget(checkbox)
            self.checks.append(checkbox)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Выбрать')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Отмена')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected(self):
        return [item.text() for item in self.checks if item.isChecked()]


class SourcesDialog(QDialog):
    def __init__(self, sources, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Источники спроса')
        self.resize(900, 600)
        self.setMinimumSize(740, 480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        heading = label('Источники и факты')
        heading.setObjectName('dialogTitle')
        layout.addWidget(heading)
        self.sources = sources
        self.table = QTableWidget(len(sources), 4)
        self.table.setObjectName('sourceEvidenceTable')
        self.table.setHorizontalHeaderLabels(['Издание', 'Дата', 'Факт', 'Ссылка'])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        enable_row_hover(self.table)
        header = self.table.horizontalHeader()
        for column in (0, 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(3, 180)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        for index, source in enumerate(sources):
            excerpt = source.get('excerpt') or source.get('fact') or source.get('text') or ''
            fact = source.get('fact') or excerpt
            values = [urlsplit(source['url']).hostname or source['url'],
                      str(source.get('published_at') or '')[:10] or '—', fact, source['url']]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.table.setItem(index, column, item)
            self.table.setRowHeight(index, max(50, self.fontMetrics().height() + 28))
        def show():
            index = self.table.currentRow()
            if 0 <= index < len(sources):
                source = sources[index]
                excerpt = source.get('excerpt') or source.get('fact') or source.get('text') or ''
                self.text.setPlainText(f"{source.get('title') or ''}\n{source['url']}\nДата: {source.get('published_at') or 'не указана'}\n\n{excerpt}")
        def open_selected():
            index = self.table.currentRow()
            if 0 <= index < len(sources):
                open_site(sources[index]['url'])
        self.table.itemSelectionChanged.connect(show)
        self.table.itemActivated.connect(lambda _: open_selected())
        layout.addWidget(self.table, 1)
        layout.addWidget(self.text, 1)
        row = QHBoxLayout()
        open_button = QPushButton('Открыть источник')
        open_button.setEnabled(bool(sources))
        open_button.clicked.connect(open_selected)
        close = QPushButton('Закрыть')
        close.clicked.connect(self.accept)
        row.addWidget(open_button)
        row.addStretch()
        row.addWidget(close)
        layout.addLayout(row)
        if sources:
            self.table.setCurrentCell(0, 0)
        else:
            self.text.setPlainText('Источников пока нет.')


class ManualContactDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Добавить контакт вручную')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        form = QFormLayout()
        self.kind = QComboBox()
        self.kind.addItem('Email', 'email')
        self.kind.addItem('Телефон', 'phone')
        self.value = QLineEdit()
        self.value.setPlaceholderText('Адрес email или номер телефона')
        self.source = QLineEdit()
        self.source.setPlaceholderText('https://страница-источник.example/')
        form.addRow('Тип контакта', self.kind)
        form.addRow('Контакт', self.value)
        form.addRow('Публичный источник', self.source)
        layout.addLayout(form)
        layout.addWidget(label('Контакт будет помечен как добавленный вручную. Разрешение на письмо задаётся отдельно.'))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('Добавить')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Отмена')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class CompanyDialog(QDialog):
    def __init__(self, store, product_id, company_id, parent=None):
        super().__init__(parent)
        self.store = store
        self.product_id = product_id
        self.company_id = company_id
        company = store.one('SELECT * FROM companies WHERE id=?', (company_id,))
        fit = store.one('SELECT * FROM company_fit WHERE product_id=? AND company_id=?', (product_id, company_id)) or {}
        self.setWindowTitle(company['name'])
        self.resize(660, 700)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        heading = label(company['name'])
        heading.setObjectName('dialogTitle')
        layout.addWidget(heading)
        if company['url']:
            site = QPushButton(company['url'])
            site.setObjectName('link')
            site.clicked.connect(lambda: open_site(company['url']))
            layout.addWidget(site)
        status_labels = {'FIT': 'Подходит', 'INSUFFICIENT_EVIDENCE': 'Нужна проверка', 'NOT_FIT': 'Не подходит'}
        details = [status_labels.get(fit.get('status'), 'Нужна проверка'),
                   fit.get('reason') or 'Добавьте сведения о деятельности компании.']
        try:
            profile = json.loads(fit.get('activity_profile') or '{}')
        except (TypeError, ValueError):
            profile = {}
        if isinstance(profile, dict):
            for activity in profile.get('activities', []):
                if isinstance(activity, dict):
                    details.append('Деятельность: ' + str(activity.get('label', '')))
                    if activity.get('quote'):
                        details.append('Факт: «' + str(activity['quote']) + '»')
        contacts = store.query('SELECT c.kind,c.value,c.checked_at,c.source_id,s.title AS source_title '
                               'FROM company_contacts c LEFT JOIN sources s ON s.id=c.source_id '
                               'WHERE c.company_id=? ORDER BY c.kind,c.id', (company_id,))
        contacts = [row for row in contacts if row['kind'] != 'email' or is_contact_email(row['value'])]
        details.append('\nКонтакты')
        details.extend(f"{row['value']} · {str(row['checked_at'] or '')[:10]}"
                       f" · {'добавлен вручную' if str(row.get('source_title') or '').startswith('Ручной источник') else 'источник не указан' if not row.get('source_id') else 'источник указан'}"
                       for row in contacts)
        if not contacts:
            details.append('Контакты не найдены.')
        self.add_contact = QPushButton('Добавить контакт с источником')
        self.add_contact.clicked.connect(self.add_manual_contact)
        layout.addWidget(self.add_contact)
        try:
            ids = [int(value) for value in json.loads(fit.get('source_ids') or '[]')]
        except (ValueError, TypeError):
            ids = []
        ids.extend(row['source_id'] for row in store.query('SELECT source_id FROM company_contacts WHERE company_id=? AND source_id IS NOT NULL', (company_id,)))
        self.sources = []
        for source_id in dict.fromkeys(ids):
            source = store.one('SELECT * FROM sources WHERE id=?', (source_id,))
            if source:
                self.sources.append(source)
                details.append(f"\n{source['title']}\n{source['url']}\nПроверено: {source['fetched_at'][:10]}\n{source['text'][:1500]}")
        self._company_details = details
        self._saved_details = '\n'.join(details)
        text = QPlainTextEdit(self._saved_details)
        self._fit_text = text
        text.setReadOnly(True)
        text.setMinimumHeight(120)
        layout.addWidget(text, 1)
        decision_form = QFormLayout()
        self.fit_status = QComboBox()
        for status, title in status_labels.items():
            self.fit_status.addItem(title, status)
        self.fit_status.setCurrentIndex(max(0, self.fit_status.findData(fit.get('status', 'INSUFFICIENT_EVIDENCE'))))
        self.fit_reason = QLineEdit()
        self.fit_reason.setMaxLength(600)
        self.fit_reason.setPlaceholderText('Почему товар подходит компании или не подходит')
        if (fit.get('fit_key') or '').startswith('manual:'):
            self.fit_reason.setText(fit.get('reason', ''))
        self._saved_fit_status = self.fit_status.currentData()
        self._saved_fit_reason = self.fit_reason.text().strip()
        self.fit_save = QPushButton('Сохранить решение')
        self.fit_save.clicked.connect(self.save_fit)
        decision_row = QHBoxLayout()
        decision_row.addWidget(self.fit_status, 1)
        decision_row.addWidget(self.fit_save)
        decision_form.addRow('Применимость товара', decision_row)
        decision_form.addRow('Причина решения', self.fit_reason)
        layout.addLayout(decision_form)
        self.fit_notice = label('')
        self.fit_notice.hide()
        layout.addWidget(self.fit_notice)
        self.fit_assess = None
        if (fit.get('status') == 'INSUFFICIENT_EVIDENCE' and hasattr(parent, 'ai')
                and not (fit.get('fit_key') or '').startswith('manual:')):
            assess = QPushButton('Оценить применимость')
            self.fit_assess = assess
            def evaluate():
                latest = store.one('SELECT fit_key FROM company_fit WHERE product_id=? AND company_id=?', (product_id, company_id)) or {}
                if (latest.get('fit_key') or '').startswith('manual:'):
                    assess.setEnabled(False)
                    return
                if not parent.secrets.get('gigachat_key'):
                    parent.guide('Для оценки применимости введите ключ GigaChat в настройках и нажмите «Проверить».', settings=True)
                    self.reject()
                    return
                assess.setEnabled(False)
                self.fit_save.setEnabled(False)
                self.fit_status.setEnabled(False)
                self.fit_reason.setEnabled(False)
                text.setPlainText('GigaChat оценивает сохранённые факты. Дождитесь результата…')
                def done(result):
                    updated = store.one('SELECT status,reason FROM company_fit WHERE product_id=? AND company_id=?', (product_id, company_id)) or {}
                    self._show_fit(updated.get('status'), updated.get('reason', 'Оценка завершена.'))
                    if self._has_unsaved_fit():
                        self.fit_notice.setText('Ручная оценка осталась в полях. Нажмите «Сохранить решение», чтобы применить её.')
                        self.fit_notice.show()
                    elif updated.get('status') == 'NOT_FIT':
                        self.fit_notice.setText('Компания доступна в фильтре «Не подходят». При необходимости сохраните ручное решение.')
                        self.fit_notice.show()
                    parent.refresh_companies()
                def failed(message):
                    text.setPlainText(message + '\n\n' + self._saved_details)
                def finished():
                    if isValid(self):
                        for control in (self.fit_save, self.fit_status, self.fit_reason):
                            control.setEnabled(True)
                        current = store.one('SELECT status,fit_key FROM company_fit WHERE product_id=? AND company_id=?', (product_id, company_id)) or {}
                        assess.setEnabled(current.get('status') == 'INSUFFICIENT_EVIDENCE'
                                          and not (current.get('fit_key') or '').startswith('manual:'))
                parent.submit(lambda emit: parent.ai.assess_fit(product_id, company_id, force=True), done,
                              owner=self, failed=failed,
                              finished=finished, busy_message='GigaChat оценивает применимость товара')
            assess.clicked.connect(evaluate)
            layout.addWidget(assess)
        if self.sources:
            row = QHBoxLayout()
            source_choice = QComboBox()
            for source in self.sources:
                source_choice.addItem(source['title'][:65] or source['url'], source['url'])
            source_button = QPushButton('Источник')
            source_button.clicked.connect(lambda: open_site(source_choice.currentData()))
            row.addWidget(source_choice, 1)
            row.addWidget(source_button)
            layout.addLayout(row)
        layout.addWidget(label('Разрешение на контакт'))
        form = QFormLayout()
        self.email = QComboBox()
        self.email.addItems([row['email'] for row in company_emails(store, company_id)])
        self.basis = QLineEdit()
        self.basis.setPlaceholderText('Например: согласие получателя')
        self.evidence = QLineEdit()
        self.evidence.setPlaceholderText('Документ, ссылка или дата и способ получения')
        self.optout = QCheckBox('Не отправлять письма')
        form.addRow('Email', self.email)
        form.addRow('Основание', self.basis)
        form.addRow('Подтверждение', self.evidence)
        form.addRow('', self.optout)
        layout.addLayout(form)
        self.restore_contact = QPushButton('Снять запрет для выбранного email')
        self.restore_contact.clicked.connect(self.restore_permission)
        layout.addWidget(self.restore_contact)
        self.load_permission(self.email.currentText())
        self.email.currentIndexChanged.connect(self._permission_recipient_changed)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('Сохранить разрешение')
        buttons.button(QDialogButtonBox.StandardButton.Close).setText('Закрыть')
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(self.email.count() > 0)
        layout.addWidget(buttons)

    def add_manual_contact(self):
        dialog = ManualContactDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        value = dialog.value.text().strip()
        source_url = dialog.source.text().strip()
        parsed = urlsplit(source_url)
        kind = dialog.kind.currentData()
        valid_url = parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username
        valid_value = is_contact_email(value) if kind == 'email' else (
            sum(char.isdigit() for char in value) >= 7 and sum(char.isdigit() for char in value) <= 15
            and all(char.isdigit() or char in '+() -.' for char in value))
        if not valid_url or not valid_value:
            self.fit_notice.setText('Укажите корректный email или телефон и публичную ссылку на источник.')
            self.fit_notice.show()
            return
        source_id = self.store.put_source(source_url, 'Ручной источник контакта',
                                          'Публичная ссылка, указанная пользователем для контакта: ' + value)
        scope = 'hq' if kind == 'email' and email_purpose(value)[1] == 'Общий' else ''
        self.store.execute('INSERT INTO company_contacts(company_id,kind,value,source_id,checked_at,scope) '
                           'VALUES(?,?,?,?,?,?) ON CONFLICT(company_id,kind,value) DO UPDATE SET '
                           'source_id=excluded.source_id,checked_at=excluded.checked_at,scope=excluded.scope',
                           (self.company_id, kind, value, source_id, now(), scope))
        previous = self.email.currentText()
        with QSignalBlocker(self.email):
            self.email.clear()
            self.email.addItems([row['email'] for row in company_emails(self.store, self.company_id)])
            index = self.email.findText(value if kind == 'email' else previous)
            if index >= 0:
                self.email.setCurrentIndex(index)
        self.load_permission(self.email.currentText())
        self.fit_notice.setText('Контакт добавлен вручную. Отдельно сохраните основание и подтверждение перед отправкой.')
        self.fit_notice.show()
        if self.parent() and hasattr(self.parent(), 'refresh_companies'):
            self.parent().refresh_companies()

    def _show_fit(self, status, reason):
        index = self.fit_status.findData(status)
        if index >= 0:
            if not self._has_unsaved_fit():
                self.fit_status.setCurrentIndex(index)
            self._saved_fit_status = status
        self._company_details[:2] = [self.fit_status.itemText(index) if index >= 0 else self.fit_status.currentText(), reason]
        self._saved_details = '\n'.join(self._company_details)
        self._fit_text.setPlainText(self._saved_details)

    def save_fit(self):
        try:
            self.store.set_company_fit_manual(self.product_id, self.company_id,
                                              self.fit_status.currentData(), self.fit_reason.text())
        except ValueError as exc:
            self.fit_notice.setText(str(exc))
            self.fit_notice.show()
            return
        self._show_fit(self.fit_status.currentData(), self.fit_reason.text().strip())
        self._saved_fit_reason = self.fit_reason.text().strip()
        self.fit_notice.setText('Решение сохранено.')
        self.fit_notice.show()
        if self.fit_assess is not None:
            self.fit_assess.setEnabled(False)
            self.fit_assess.setToolTip('Сохранено ручное решение пользователя.')
        if hasattr(self.parent(), 'refresh_companies'):
            self.parent().refresh_companies()

    def _permission_fields(self):
        return self.basis.text(), self.evidence.text(), self.optout.isChecked()

    def _permission_recipient_changed(self, _index):
        if self._permission_fields() != self._saved_permission_fields and not confirm(
                self, 'Несохранённое разрешение',
                'При смене адреса введённые данные разрешения будут потеряны. '
                'Нажмите «Отмена», чтобы вернуться к прежнему адресу и сохранить изменения.',
                'Сменить без сохранения'):
            with QSignalBlocker(self.email):
                self.email.setCurrentIndex(self._permission_index)
            return
        self.load_permission(self.email.currentText())

    def load_permission(self, email):
        row = self.store.one('SELECT * FROM permissions WHERE company_id=? AND lower(email)=lower(?) ORDER BY id DESC LIMIT 1', (self.company_id, email))
        self.basis.setText(row['basis'] if row else '')
        self.evidence.setText(row['evidence'] if row else '')
        self.optout.setChecked(bool(self.store.one('SELECT email FROM opt_out WHERE lower(email)=lower(?)', (email,))))
        self.restore_contact.setEnabled(self.optout.isChecked())
        self._permission_index = self.email.currentIndex()
        self._saved_permission_fields = self._permission_fields()

    def restore_permission(self):
        email = self.email.currentText().strip().lower()
        if not email or not self.store.one('SELECT email FROM opt_out WHERE lower(email)=lower(?)', (email,)):
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('Новое согласие')
        layout = QVBoxLayout(dialog)
        layout.addWidget(label('Снять запрет только для ' + email + '. Укажите новое согласие, полученное после отказа.'))
        proof = QLineEdit()
        proof.setPlaceholderText('Дата, способ получения и подтверждение нового согласия')
        layout.addWidget(proof)
        confirmed = QCheckBox('Подтверждаю, что согласие новое и получено после отказа')
        layout.addWidget(confirmed)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if not dialog.exec():
            return
        evidence = proof.text().strip()
        if not confirmed.isChecked() or not evidence:
            QMessageBox.information(self, 'Новое согласие', 'Укажите подтверждение нового согласия и отметьте его получение после отказа.')
            return
        if not confirm(self, 'Снять запрет?', 'Снять запрет только для ' + email + ' на основании нового согласия?', 'Снять запрет'):
            return
        with self.store.db as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT email FROM opt_out WHERE lower(email)=lower(?)', (email,)).fetchone()
            if not current:
                return
            db.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)',
                       (self.company_id, email, 'Новое согласие', evidence, now()))
            db.execute('DELETE FROM opt_out WHERE lower(email)=lower(?)', (email,))
            db.execute('INSERT INTO audit_events(event,details,created_at) VALUES(?,?,?)',
                       ('mail.opt_out_removed', email + ': новое согласие — ' + evidence, now()))
        self.load_permission(email)

    def _has_unsaved_fit(self):
        return (self.fit_status.currentData() != self._saved_fit_status
                or self.fit_reason.text().strip() != self._saved_fit_reason)

    def _has_unsaved_permission(self):
        return self._permission_fields() != getattr(self, '_saved_permission_fields', self._permission_fields())

    def _confirm_close(self, action, *, check_permission=False):
        if self._has_unsaved_fit():
            if not confirm(self, 'Несохранённая оценка',
                           'Изменения оценки применимости будут потеряны. '
                           'Чтобы сохранить их, вернитесь в карточку и нажмите «Сохранить решение».', action):
                return False
        if check_permission and self._has_unsaved_permission():
            return confirm(self, 'Несохранённое разрешение',
                           'Введённые основание или подтверждение разрешения не сохранены. '
                           'Закрытие карточки удалит эти изменения.', action)
        return True

    def reject(self):
        if self._confirm_close('Закрыть без сохранения оценки', check_permission=True):
            super().reject()

    def save(self):
        if not self._confirm_close('Сохранить без оценки'):
            return
        email = self.email.currentText().strip().lower()
        if self.optout.isChecked():
            self.store.execute('INSERT INTO opt_out(email,reason,created_at) VALUES(?,?,?) ON CONFLICT(email) DO UPDATE SET reason=excluded.reason', (email, 'Запрет пользователя', now()))
        else:
            existing = self.store.one('SELECT email FROM opt_out WHERE lower(email)=lower(?)', (email,))
            if existing:
                QMessageBox.information(self, 'Контакт запрещён', 'Запрет сохранён. Для его отмены нужно новое подтверждённое согласие.')
                return
            if not self.basis.text().strip() or not self.evidence.text().strip():
                QMessageBox.information(self, 'Разрешение', 'Укажите основание и его подтверждение.')
                return
            self.store.execute('INSERT INTO permissions(company_id,email,basis,evidence,created_at) VALUES(?,?,?,?,?)', (self.company_id, email, self.basis.text().strip(), self.evidence.text().strip(), now()))
        self.accept()


class HistoryDialog(QDialog):
    def __init__(self, store, product_id, company_id, parent=None):
        super().__init__(parent)
        self.setWindowTitle('История переписки')
        self.resize(720, 600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        heading = label('История переписки')
        heading.setObjectName('dialogTitle')
        layout.addWidget(heading)
        messages = store.query('''SELECT m.*,a.status AS attempt_status FROM mail_messages m
            LEFT JOIN outbound_attempts a ON a.message_id=m.message_id
            WHERE m.product_id=? AND m.company_id=? ORDER BY m.sent_at,m.id''', (product_id, company_id))
        self.list = QListWidget()
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        for message in messages:
            direction = 'Получено' if message['direction'] in ('incoming', 'inbound') else {
                'sent': 'Отправлено', 'failed': 'Не отправлено', 'sending': 'Отправляется',
                'uncertain': 'Проверяется',
            }.get(message['attempt_status'], 'Исходящее')
            self.list.addItem(f"{message['sent_at'][:16]} · {direction} · {message['subject']}")
        def select(index):
            if index < 0:
                return
            message = messages[index]
            self.text.setPlainText(f"От: {message['sender']}\nКому: {message['recipient']}\nТема: {message['subject']}\n\n{message['body']}")
        self.list.currentRowChanged.connect(select)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.text, 3)
        if messages:
            self.list.setCurrentRow(0)
        else:
            self.text.setPlainText('Переписки пока нет.')
        close = QPushButton('Закрыть')
        close.clicked.connect(self.accept)
        layout.addWidget(close)


class OfferDialog(QDialog):
    def __init__(self, window, company_id):
        super().__init__(window)
        self.window = window
        self.store = window.store
        self.product_id = window.product_id
        self.company_id = company_id
        self.offer = None
        self.setWindowTitle('Предпросмотр УТП')
        self.resize(740, 640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        heading = label('Коммерческое предложение')
        heading.setObjectName('dialogTitle')
        layout.addWidget(heading)
        layout.addWidget(label('Кому'))
        self.recipient_choice = QComboBox()
        self.recipient_choice.setObjectName('offerRecipient')
        contacts = company_emails(self.store, company_id)
        for contact in contacts:
            mark = ' · головной офис' if contact['purpose'] == 'Общий' else ''
            self.recipient_choice.addItem(contact['email'] + ' · ' + contact['purpose'] + mark, contact['email'])
        preferred = next((index for index, contact in enumerate(contacts) if contact['purpose'] != 'Общий'), -1)
        if len(contacts) == 1:
            # The only available address still needs an explicit send confirmation.
            self.recipient_choice.setCurrentIndex(0)
            self.recipient = contacts[0]['email']
        elif preferred >= 0:
            self.recipient_choice.setCurrentIndex(preferred)
            self.recipient = contacts[preferred]['email']
        else:
            self.recipient_choice.setCurrentIndex(-1)
            self.recipient = ''
        self.recipient_choice.setPlaceholderText(
            'Выберите адрес' if contacts and self.recipient_choice.currentIndex() < 0 else
            ('Email не найден' if not contacts else 'Кому'))
        self.recipient_choice.currentIndexChanged.connect(self.recipient_changed)
        layout.addWidget(self.recipient_choice)
        self.subject = QLineEdit()
        self.subject.setPlaceholderText('Тема письма')
        self.subject.setMaxLength(MAX_SUBJECT_LENGTH)
        layout.addWidget(self.subject)
        self.body = QPlainTextEdit()
        self.body.setPlaceholderText('Готовим персональное предложение…')
        layout.addWidget(self.body, 1)
        self.notice = label('')
        self.notice.setObjectName('notice')
        layout.addWidget(self.notice)
        self.settings_button = QPushButton('Открыть настройки')
        self.settings_button.clicked.connect(self.open_settings)
        self.settings_button.hide()
        layout.addWidget(self.settings_button)
        row = QHBoxLayout()
        self.regenerate = QPushButton('Переписать')
        self.save_button = QPushButton('Сохранить')
        self.send_button = QPushButton('Отправить')
        self.send_button.setObjectName('primary')
        close = QPushButton('Закрыть')
        close.clicked.connect(self.reject)
        self.regenerate.clicked.connect(self.regenerate_offer)
        self.save_button.clicked.connect(self.save)
        self.send_button.clicked.connect(self.send)
        row.addWidget(self.regenerate)
        row.addStretch()
        row.addWidget(self.save_button)
        row.addWidget(close)
        row.addWidget(self.send_button)
        layout.addLayout(row)
        self.load_offer()

    def set_busy(self, busy):
        locked = self.delivery_locked()
        for widget in (self.subject, self.body, self.recipient_choice, self.save_button, self.send_button, self.regenerate):
            widget.setEnabled(not busy and not locked)
        if busy:
            notice_tone(self.notice, 'info')
            self.notice.setText('GigaChat готовит предложение по сохранённым фактам. Дождитесь результата…')

    def delivery_locked(self):
        return bool(self.offer and self.store.one("""SELECT id FROM outbound_attempts
            WHERE offer_id=? AND status IN ('sending','uncertain','sent') LIMIT 1""", (self.offer['id'],)))

    def recipient_changed(self, *_):
        self.recipient = self.recipient_choice.currentData() or ''
        if self.recipient and getattr(self, '_missing_recipient', False):
            self._missing_recipient = False
            self.notice.setText('Выбран новый получатель. Проверьте письмо и подтвердите отправку.')
        if self.offer and not self.delivery_locked():
            self.store.execute("""UPDATE offers SET approved=0 WHERE id=? AND NOT EXISTS
                (SELECT 1 FROM outbound_attempts WHERE offer_id=offers.id
                 AND status IN ('sending','uncertain','sent'))""", (self.offer['id'],))

    def open_settings(self):
        self.window.open_settings()
        self.reject()

    def load_offer(self):
        row = self.store.one('SELECT * FROM offers WHERE product_id=? AND company_id=?', (self.product_id, self.company_id))
        if row:
            self.loaded(row)
        elif not self.window.secrets.get('gigachat_key'):
            timestamp = now()
            offer_id = self.store.execute(
                'INSERT INTO offers(product_id,company_id,subject,body,reason,source_ids,created_at,updated_at) '
                'VALUES(?,?,\'\',\'\',?,\'[]\',?,?)',
                (self.product_id, self.company_id, 'Черновик создан вручную.', timestamp, timestamp))
            self.loaded(self.store.one('SELECT * FROM offers WHERE id=?', (offer_id,)))
            self.notice.setText('Пустой черновик создан. Заполните тему и текст вручную; для УТП из источников подключите GigaChat.')
        else:
            self.set_busy(True)
            self.window.submit(lambda emit: self.window.ai.generate_offer(self.product_id, self.company_id), self.loaded,
                               owner=self, failed=self.generation_error, busy_message='GigaChat составляет коммерческое предложение')

    def loaded(self, row):
        self.offer = row
        self._missing_recipient = False
        self.subject.setText(row['subject'])
        self.body.setPlainText(row['body'])
        if row.get('recipient'):
            index = self.recipient_choice.findData(row['recipient'])
            self._missing_recipient = index < 0
            self.recipient_choice.setPlaceholderText('Выберите другой email' if self._missing_recipient and self.recipient_choice.count() else 'Email не найден')
            self.recipient_choice.setCurrentIndex(index)
            self.recipient = self.recipient_choice.currentData() or ''
        self.set_busy(False)
        notice_tone(self.notice, 'info')
        self.notice.setText('Проверьте текст. Отправка — только после подтверждения.')
        if self.delivery_locked():
            self.notice.setText('Письмо уже отправлено или его отправка проверяется. Текст доступен для просмотра.')
            if self._missing_recipient:
                self.recipient_choice.setPlaceholderText(row['recipient'])
        elif self._missing_recipient:
            self.notice.setText('Прежний email ' + row['recipient'] + ' больше не доступен в контактах. '
                                + ('Выберите другой адрес получателя.' if self.recipient_choice.count() else 'Добавьте подтверждённый контакт компании.'))
            notice_tone(self.notice, 'warning')

    def generation_error(self, message):
        self.set_busy(False)
        if self.offer is None:
            self.save_button.setEnabled(False)
            self.send_button.setEnabled(False)
        notice_tone(self.notice, 'error')
        self.notice.setText(message)
        self.settings_button.setVisible('ключ' in message.casefold() or 'подключ' in message.casefold() or 'квот' in message.casefold())

    def regenerate_offer(self):
        if self.delivery_locked():
            return
        if not confirm(self, 'Переписать УТП?', 'Текущий текст будет заменён. Продолжить?', 'Переписать'):
            return
        self.set_busy(True)
        # Explicit regeneration never silently overwrites an approved or edited offer.
        def task(emit):
            return self.window.ai.generate_offer(self.product_id, self.company_id, force=True)
        self.window.submit(task, self.loaded, owner=self, failed=self.generation_error,
                           busy_message='GigaChat составляет коммерческое предложение')

    def save(self, approved=False):
        if not self.offer:
            return False
        subject, body = self.subject.text().strip(), self.body.toPlainText().strip()
        if '\n' in subject or '\r' in subject:
            self.notice.setText('Тема письма должна занимать одну строку.')
            return False
        if len(subject) > MAX_SUBJECT_LENGTH:
            self.notice.setText(f'Тема письма — не больше {MAX_SUBJECT_LENGTH} символов.')
            return False
        try:
            save_offer(self.store, self.offer['id'], subject, body, self.recipient, approved)
        except ValueError as exc:
            self.notice.setText(str(exc))
            self.set_busy(False)
            return False
        self.notice.setText('Черновик сохранён.')
        return True

    def send(self):
        if self.window.mail_busy:
            self.notice.setText('Дождитесь проверки почты.')
            return
        fit = self.store.one('SELECT status FROM company_fit WHERE product_id=? AND company_id=?',
                             (self.product_id, self.company_id))
        if not fit or fit['status'] != 'FIT':
            self.notice.setText('Подтвердите применимость товара в карточке компании.')
            return
        account = self.store.one('SELECT connected FROM mail_accounts WHERE id=1')
        if not account or not account['connected']:
            self.notice.setText('Для отправки подключите почту в настройках: email, пароль приложения → «Подключить».')
            self.settings_button.show()
            return
        if not self.recipient:
            self.notice.setText('Выберите адрес получателя в поле «Кому».' if self.recipient_choice.count() else
                                'У компании нет email. Отправка станет доступна после добавления подтверждённого контакта.')
            return
        if not self.subject.text().strip() or not self.body.toPlainText().strip():
            self.notice.setText('Заполните тему и текст письма перед отправкой.')
            return
        permission = self.store.one('SELECT id FROM permissions WHERE company_id=? AND lower(email)=lower(?) AND basis<>\'\' AND evidence<>\'\' LIMIT 1', (self.company_id, self.recipient))
        if not permission or self.store.one('SELECT email FROM opt_out WHERE lower(email)=lower(?)', (self.recipient,)):
            self.notice.setText('В карточке компании укажите основание и подтверждение разрешения на контакт. При запрете отправка недоступна.')
            return
        if not self.save():
            return
        if not confirm(self, 'Отправить письмо?', f'Адресат: {self.recipient}\nОтправить проверенный текст?', 'Отправить'):
            return
        if not self.save(approved=True):
            return
        expected = {'subject': self.subject.text().strip(), 'body': self.body.toPlainText().strip(),
                    'recipient': self.recipient,
                    'sender': self.store.one('SELECT email FROM mail_accounts WHERE id=1')['email']}
        selected_recipient, offer_id = self.recipient, self.offer['id']
        self.set_busy(True)
        self.notice.setText('Отправляем письмо. При неизвестном результате повторная отправка автоматически не выполняется.')
        self.window.mail_busy = True
        def done(result):
            notice_tone(self.notice, 'success')
            self.notice.setText(str(result))
            self.set_busy(False)
            self.window.refresh_companies()
            self.send_button.setEnabled(False)
        def failed(message):
            notice_tone(self.notice, 'error')
            self.notice.setText(message)
            self.set_busy(False)
            self.settings_button.setVisible(any(word in message.casefold() for word in ('подключ', 'парол', 'gmail', 'почт', 'настройк')))
        worker = self.window.submit(
            lambda emit: self.window.mail.send_offer(offer_id, recipient=selected_recipient, expected=expected), done,
            owner=self, failed=failed, finished=self.window.mail_finished, busy_message='Отправляем письмо')
        if worker is None:
            self.window.mail_busy = False
            self.set_busy(False)
            self.notice.setText('Не удалось начать отправку: дождитесь завершения другой операции и попробуйте снова.')
