"""Three concise native tabs; background network work and persistent product branches."""
import json
import os
import re
import sys
import threading
import uuid
from pathlib import Path

from PySide6.QtCore import Qt, QLockFile, QSize, QThreadPool, QTimer
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QStackedWidget, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from sales_manager import __version__
from sales_manager.ai import AIService
from sales_manager.mail import MailService
from sales_manager.research import COUNTRIES, ResearchService, normalize_product
from sales_manager.research.market import import_statistics, market_evidence, market_ranking
from sales_manager.research.providers import RSSProvider
from sales_manager.research.reporting import market_summary
from sales_manager.storage import Secrets, Store, now
from sales_manager.storage.settings_save import apply_settings, validate_extra_urls
from sales_manager.core.contacts import company_emails
from sales_manager.core.company_listing import visible_companies
from .dialogs import CompanyDialog, CountriesDialog, HistoryDialog, OfferDialog, SourcesDialog, label, open_site
from .theme import COLORS, STYLE, DemandDelegate, demand_background, enable_row_hover, font as theme_font, notice_tone, palette as theme_palette
from .workers import Worker
from .busy import BusyOverlay


KEYED_SOURCES = {'tavily': 'Tavily', 'serper': 'Serper', 'exa': 'Exa', 'firecrawl': 'Firecrawl', 'jina': 'Jina Search'}
KEYLESS_SOURCES = {'overpass': 'OpenStreetMap / Overpass', 'gdelt': 'GDELT', 'wikidata': 'Wikidata', 'mediawiki': 'Wikipedia', 'rss': 'RSS', 'comtrade': 'UN Comtrade · статистика торговли', 'catalog': 'Открытые каталоги', 'procurement': 'Публикации закупок'}


class CellButton(QPushButton):
    """Table column sizing must include the button's CSS insets and full label."""
    def sizeHint(self):
        hint = super().sizeHint()
        name = self.objectName()
        if name in ('cellAction', 'tableLink'):
            inset = 44 if name == 'cellAction' else 28
            hint.setWidth(max(hint.width(), self.fontMetrics().horizontalAdvance(self.text()) + inset))
        return hint

    def minimumSizeHint(self):
        return self.sizeHint()


class CompanyLink(QPushButton):
    """Long company names stay inside their own cell with an explicit ellipsis."""
    def __init__(self, text):
        super().__init__(text)
        self.full_text = text
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def resizeEvent(self, event):
        self.setText(self.fontMetrics().elidedText(self.full_text, Qt.TextElideMode.ElideRight, max(20, self.width() - 20)))
        super().resizeEvent(event)


def button(text, callback, primary=False):
    item = CellButton(text)
    if primary:
        item.setObjectName('primary')
    item.clicked.connect(callback)
    return item


def cell_control(table, row, column, control):
    """Reserve both button insets and the table's own cell padding."""
    table.setCellWidget(row, column, control)
    control.ensurePolished()
    hint = control.sizeHint()
    control.setMinimumWidth(hint.width())
    spacer = QTableWidgetItem('')
    spacer.setSizeHint(QSize(hint.width() + 24, max(50, hint.height() + 8)))
    table.setItem(row, column, spacer)


def field(placeholder='', secret=False):
    item = QLineEdit()
    item.setPlaceholderText(placeholder)
    if secret:
        item.setEchoMode(QLineEdit.EchoMode.Password)
    return item


def spin(minimum, maximum, value):
    item = QSpinBox()
    item.setRange(minimum, maximum)
    item.setValue(value)
    return item


def configure_application(application):
    """Apply one theme to windows, popups and dialogs in every launch mode."""
    application.setApplicationName('SalesManagerFirst')
    application.setOrganizationName('Argon')
    application.setApplicationVersion(__version__)
    application.setStyle('Fusion')
    application.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    application.setPalette(theme_palette())
    application.setFont(theme_font())
    application.setStyleSheet(STYLE)
    application.setWindowIcon(QIcon(str(Path(__file__).resolve().parents[1] / 'assets' / 'app.ico')))


class Window(QMainWindow):
    def __init__(self, store=None, secrets=None):
        super().__init__()
        configure_application(QApplication.instance())
        self.store = store or Store()
        self.secrets = secrets or Secrets()
        self.research = ResearchService(self.store, self.secrets)
        self.ai = AIService(self.store, self.secrets)
        self.research.ai = self.ai
        self.mail = MailService(self.store, self.secrets)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self.workers = set()
        self.research_cancel = None
        self.research_kind = None
        self.mail_busy = False
        self.search_check_busy = False
        self._closing = False
        self.product_id = None
        self.company_rows = []
        self.countries = self.store.get_setting('research.countries', list(COUNTRIES))
        self.setWindowTitle('SALES MANAGER FIRST')
        self.resize(1200, 780)
        self.setMinimumSize(1020, 680)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(22, 18, 22, 12)
        header = QHBoxLayout()
        title = label('SALES MANAGER FIRST')
        title.setObjectName('title')
        title.setWordWrap(False)
        header.addWidget(title)
        header.addStretch()
        tagline = label('Рынок → товар → клиент')
        tagline.setObjectName('muted')
        tagline.setWordWrap(False)
        header.addWidget(tagline)
        layout.addLayout(header)
        self.tabs = QTabWidget()
        self.tabs.setObjectName('mainTabs')
        layout.addWidget(self.tabs, 1)
        self.action_row = QWidget()
        action_layout = QHBoxLayout(self.action_row)
        action_layout.setContentsMargins(0, 0, 0, 0)
        self.action_notice = label('')
        self.action_notice.setObjectName('notice')
        action_layout.addWidget(self.action_notice, 1)
        layout.insertWidget(1, self.action_row)
        self.action_row.hide()
        self.busy_indicator = QProgressBar()
        self.busy_indicator.setRange(0, 0)
        self.busy_indicator.setTextVisible(False)
        self.busy_indicator.setFixedWidth(90)
        self.statusBar().hide()
        self.statusBar().messageChanged.connect(self.status_notice)
        self.busy_indicator.hide()
        self.build_market()
        self.build_companies()
        self.build_settings()
        self.busy_overlay = BusyOverlay(self)
        self._foreground_worker = None
        self._recovery_attempted = set()
        self._auto_recovery_enabled = True
        self.refresh_branches()
        self.refresh_market()
        self.refresh_companies()
        self.update_resume_labels()
        self.timer = QTimer(self)
        self.timer.setInterval(180_000)
        self.timer.timeout.connect(self.sync_mail)
        self.timer.start()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(2000)
        self.refresh_timer.timeout.connect(self.refresh_research)
        self.refresh_timer.start()
        self.close_timer = QTimer(self)
        self.close_timer.setInterval(150)
        self.close_timer.timeout.connect(self._finish_close)
        self.startup_mail_timer = QTimer(self)
        self.startup_mail_timer.setSingleShot(True)
        self.startup_mail_timer.timeout.connect(self.sync_mail)
        self.startup_mail_timer.start(800)
        self.recovery_timer = QTimer(self)
        self.recovery_timer.setSingleShot(True)
        self.recovery_timer.timeout.connect(self.recover_research)
        self.recovery_timer.start(400)

    def submit(self, function, success=None, owner=None, failed=None, finished=None, busy_message=None, silent=False, cancel=None):
        if busy_message and self._foreground_worker is not None:
            return None
        worker = Worker(function, cleanup=self.store.close)
        self.workers.add(worker)
        owner = owner or self
        if busy_message:
            self._foreground_worker = worker
            self.tabs.setEnabled(False)
            if owner is not self:
                owner.setEnabled(False)
            self.busy_overlay.start(busy_message, owner, cancel=cancel)
        def release_busy():
            if self._foreground_worker is worker and isValid(self):
                self._foreground_worker = None
                self.busy_overlay.stop()
                if not self._closing:
                    self.tabs.setEnabled(True)
                    if owner is not self and isValid(owner):
                        owner.setEnabled(True)
        def alive():
            return not self._closing and isValid(self) and isValid(owner)
        def completed(result):
            release_busy()
            if alive():
                if success:
                    success(result)
                if isinstance(result, str) and not silent:
                    self.statusBar().showMessage(result[:220], 15000)
        def error(message):
            release_busy()
            if alive():
                if failed:
                    failed(message)
                else:
                    self.guide(message, settings='настройк' in message.casefold() or 'ключ' in message.casefold() or 'источник' in message.casefold(), tone='error')
        def progress(message):
            if not self._closing and isValid(self):
                if busy_message:
                    self.busy_overlay.message.setText(self.operation_stage(message, busy_message))
        def cleanup():
            release_busy()
            self.workers.discard(worker)
            if not isValid(self):
                return
            if not self.workers and isValid(self.busy_indicator):
                self.busy_indicator.hide()
            if finished:
                finished()
        worker.signals.succeeded.connect(completed)
        worker.signals.failed.connect(error)
        worker.signals.progress.connect(progress)
        worker.signals.finished.connect(cleanup)
        self.pool.start(worker)
        return worker

    @staticmethod
    def operation_stage(message, default):
        """Only business stages reach the card; provider details stay in diagnostics."""
        text = str(message).casefold()
        if any(word in text for word in ('gigachat', 'смыслов')):
            return 'GigaChat анализирует найденные сведения'
        if any(word in text for word in ('контакт', 'изучаем сайт', 'обрабатываем страниц')):
            return 'Изучаем сайты и собираем контакты'
        return default

    def guide(self, message, settings=False, tone='info'):
        """Keep the next action visible instead of silently ignoring a click."""
        notice_tone(self.action_notice, tone)
        self.action_notice.setText(message)
        self.action_row.show()
        self.statusBar().showMessage(message, 20000)

    def status_notice(self, message):
        if not message or self._closing:
            return
        if self.research_cancel is not None:
            return
        if hasattr(self, 'action_notice'):
            # guide() also writes the status bar; keep its tone for the same text.
            # A different later status restores the ordinary notice color.
            if self.action_notice.text() != message:
                notice_tone(self.action_notice, 'info')
            self.action_notice.setText(message)
            self.action_row.show()

    def open_settings(self):
        self.tabs.setCurrentIndex(2)

    def clear_guide(self):
        self.action_row.hide()

    def build_market(self):
        page = QWidget()
        page.setObjectName('marketPage')
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        row = QHBoxLayout()
        self.country_button = button(self.country_text(), self.choose_countries)
        self.market_start = button('Найти товары', self.start_market, True)
        self.market_pause = button('Пауза', self.pause_research)
        self.market_pause.setEnabled(False)
        self.market_pause.hide()
        row.addWidget(self.country_button)
        row.addWidget(self.market_start)
        row.addWidget(self.market_pause)
        row.addWidget(self.busy_indicator)
        row.addStretch()
        layout.addLayout(row)
        self.search_mode_label = label('Режим поиска: базовый — без API-ключей')
        self.search_mode_label.setObjectName('muted')
        layout.addWidget(self.search_mode_label)
        self.market_research_notice = label('')
        self.market_research_notice.setObjectName('warningNotice')
        self.market_research_notice.hide()
        layout.addWidget(self.market_research_notice)
        self.onboarding = label('Нажмите «Найти товары». Выбор товара откроет его компании.')
        self.onboarding.setObjectName('notice')
        layout.addWidget(self.onboarding)
        self.market_table = QTableWidget(0, 3)
        self.market_table.setObjectName('marketTable')
        self.market_table.setHorizontalHeaderLabels(['Товар', 'Спрос', 'Источники'])
        self.market_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.market_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.market_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.market_table.verticalHeader().hide()
        self.market_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.market_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.market_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.market_table.setWordWrap(False)
        self.market_table.setAlternatingRowColors(True)
        self.market_table.setShowGrid(False)
        enable_row_hover(self.market_table)
        self.market_table.setItemDelegateForColumn(1, DemandDelegate(self.market_table))
        self.market_table.horizontalHeader().setMaximumSectionSize(360)
        self.market_table.itemClicked.connect(lambda item: self.select_market_product() if item.column() == 0 else None)
        self.market_table.itemActivated.connect(lambda item: self.select_market_product() if item.column() < 2 else None)
        layout.addWidget(self.market_table, 1)
        hint = label('Выберите товар, чтобы открыть его компании.')
        hint.setObjectName('muted')
        layout.addWidget(hint)
        self.market_hint = hint
        self.tabs.addTab(page, 'Рынок')

    def country_text(self):
        return 'Россия + СНГ' if len(self.countries) == len(COUNTRIES) else ', '.join(self.countries[:2]) + ('…' if len(self.countries) > 2 else '')

    def choose_countries(self):
        dialog = CountriesDialog(COUNTRIES, self.countries, self)
        if dialog.exec():
            selected = dialog.selected()
            if not selected:
                self.statusBar().showMessage('Выберите хотя бы одну страну.', 10000)
                return
            self.countries = selected
            self.store.set_setting('research.countries', selected)
            self.country_button.setText(self.country_text())
            self.refresh_market()
            self.restore_research_notices()

    def refresh_market(self):
        ranking = market_ranking(self.store, self.countries)
        self.market_rows = ranking['rows']
        self.market_caption = ('По данным публикаций · независимые источники и свежесть · 90 дней'
                               if ranking['preliminary'] else
                               'Спрос относительно лидера среди сопоставимых показателей · 90 дней')
        if hasattr(self, 'market_hint'):
            self.market_hint.setText(self.market_caption)
            self.market_hint.setToolTip(ranking.get('caption', self.market_caption))
        for index in range(self.market_table.rowCount()):
            previous = self.market_table.cellWidget(index, 2)
            if previous is not None:
                previous.hide()
                self.market_table.removeCellWidget(index, 2)
        self.market_table.setRowCount(len(self.market_rows))
        for index, row in enumerate(self.market_rows):
            name = QTableWidgetItem(row['name'])
            name.setData(Qt.ItemDataRole.UserRole, row['id'])
            font = name.font()
            font.setUnderline(True)
            name.setFont(font)
            name.setToolTip('Открыть компании для товара «' + row['name'] + '»')
            basis = row.get('basis') or 'Подтверждённый спрос'
            percent = row.get('demand_percent')
            evidence = QTableWidgetItem(basis if percent is None else f'{percent:.1f}%'.replace('.0%', '%').replace('.', ','))
            evidence.setToolTip(basis + ('\nНет сопоставимых показателей для расчёта процента.' if percent is None else
                                         '\nПроцент относительно лидера среди сопоставимых показателей.'))
            if percent is not None:
                evidence.setBackground(demand_background(percent))
            self.market_table.setItem(index, 0, name)
            self.market_table.setItem(index, 1, evidence)
            sources = button('Источники · ' + str(row.get('count', len(row.get('source_ids', [])))),
                             lambda checked=False, product_id=row['id']: self.market_sources(product_id))
            sources.setObjectName('cellAction')
            sources.setToolTip('Факты и ссылки, на которых основана эта строка')
            cell_control(self.market_table, index, 2, sources)
            self.market_table.setRowHeight(index, max(50, sources.sizeHint().height() + 8, self.fontMetrics().height() + 20))
        self.onboarding.setVisible(not self.market_rows)

    def market_sources(self, product_id=None):
        if product_id is None:
            row = self.market_table.currentRow()
            if row < 0 or row >= len(self.market_rows):
                self.statusBar().showMessage('Выберите товар.', 6000)
                return
            product_id = self.market_rows[row]['id']
        sources = market_evidence(self.store, product_id, self.countries)
        SourcesDialog(sources, self).exec()

    def show_import_statistics(self):
        sources = import_statistics(self.store, self.countries)
        if not sources:
            QMessageBox.information(self, 'Статистика импорта', 'Сохранённой статистики импорта пока нет.')
            return
        dialog = SourcesDialog(sources, self)
        dialog.setWindowTitle('Импорт UN Comtrade — отдельная статистика, не спрос')
        dialog.exec()

    def add_product(self):
        text = self.branches.currentText().strip()
        try:
            from sales_manager.research.normalization import branch_identity, clean_text
            normalized = branch_identity(text)
            # Older versions may have stored a misclassified device in its own branch.
            previous_branch = 'branch:' + clean_text(text).strip(' .,;:!?')
            product = None
            if previous_branch != normalized:
                product = self.store.one('SELECT id FROM products WHERE normalized=?', (previous_branch,))
            product = product or self.store.ensure_product(text, normalized)
        except (ValueError, RuntimeError) as exc:
            self.guide(str(exc), tone='error')
            return
        self.open_product(product['id'])
        return product['id']

    def select_market_product(self):
        row = self.market_table.currentRow()
        if row < 0:
            self.statusBar().showMessage('Выберите товар.', 10000)
            return
        self.open_product(self.market_rows[row]['id'])

    def open_product(self, product_id):
        ids = self.store.get_setting('product.branches', [])
        if product_id not in ids:
            ids.append(product_id)
            self.store.set_setting('product.branches', ids)
        self.store.set_setting('product.active', product_id)
        self.refresh_branches()
        self.tabs.setCurrentIndex(1)

    def build_companies(self):
        page = QWidget()
        page.setObjectName('companyPage')
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        row = QHBoxLayout()
        self.branches = QComboBox()
        self.branches.setObjectName('productBranches')
        self.branches.setMinimumWidth(180)
        self.branches.setEditable(True)
        self.branches.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.branches.lineEdit().setPlaceholderText('Выберите или введите товар → Enter')
        self.branches.lineEdit().setMaxLength(160)
        self.branches.lineEdit().returnPressed.connect(self.add_product)
        self.branches.setToolTip('Выберите сохранённый товар или введите свой и нажмите Enter')
        self.branches.currentIndexChanged.connect(self.branch_changed)
        self.branches.editTextChanged.connect(self.branch_changed)
        self.company_start = button('Найти компании', self.start_companies, True)
        self.company_pause = button('Пауза', self.pause_research)
        self.company_pause.setEnabled(False)
        self.company_pause.hide()
        self.company_more = QPushButton('Ещё')
        self.company_more.setObjectName('more')
        more = QMenu(self.company_more)
        self.manual_company = more.addAction('Добавить компанию по ссылке')
        self.manual_company.triggered.connect(self.add_company)
        self.refresh_mail_button = more.addAction('Проверить ответы')
        self.refresh_mail_button.triggered.connect(lambda: self.sync_mail(manual=True))
        self.company_more.setMenu(more)
        row.addWidget(self.branches, 1)
        row.addWidget(self.company_start)
        row.addWidget(self.company_pause)
        row.addWidget(self.company_more)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.company_search = field('Поиск по компаниям')
        self.company_search.textChanged.connect(self.refresh_companies)
        self.company_filter = QComboBox()
        self.company_filter.addItems(['Все', 'Подходят', 'Нужна проверка', 'Без УТП', 'Отправлено', 'Ответили', 'Не подходят'])
        self.company_filter.currentIndexChanged.connect(self.refresh_companies)
        row.addWidget(self.company_search, 1)
        row.addWidget(self.company_filter)
        layout.addLayout(row)
        self.company_empty = label('Выберите товар в «Рынке» или введите свой выше.')
        self.company_empty.setObjectName('notice')
        layout.addWidget(self.company_empty)
        self.company_research_notice = label('')
        self.company_research_notice.setObjectName('warningNotice')
        self.company_research_notice.hide()
        layout.addWidget(self.company_research_notice)
        self.company_table = QTableWidget(0, 6)
        self.company_table.setObjectName('companyTable')
        self.company_table.setHorizontalHeaderLabels(['Компания', 'Email', 'Телефон', 'УТП', 'Ответ', 'История'])
        self.company_table.verticalHeader().hide()
        self.company_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.company_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.company_table.setWordWrap(False)
        self.company_table.setAlternatingRowColors(True)
        self.company_table.setShowGrid(False)
        enable_row_hover(self.company_table)
        self.company_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 6):
            self.company_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.company_table.cellDoubleClicked.connect(self.company_details)
        layout.addWidget(self.company_table, 1)
        self.tabs.addTab(page, 'Компании')

    def refresh_branches(self):
        ids = self.store.get_setting('product.branches', [])
        active = self.store.get_setting('product.active')
        products = self.store.query('SELECT * FROM products ORDER BY id')
        self.branches.blockSignals(True)
        self.branches.clear()
        self.branches.addItem('', None)
        for product in products:
            if product['id'] in ids:
                self.branches.addItem(product['name'], product['id'])
        selected = self.branches.findData(active)
        self.branches.setCurrentIndex(max(0, selected))
        self.branches.blockSignals(False)
        self.branch_changed()

    def branch_changed(self, *_):
        product_id = self.branches.currentData() if self.branches.currentText() == self.branches.itemText(self.branches.currentIndex()) else None
        self.company_start.setEnabled(bool(product_id) and self.research_cancel is None)
        self.manual_company.setEnabled(bool(product_id) and self.research_cancel is None)
        if product_id == self.product_id:
            return
        self.product_id = product_id
        self.store.set_setting('product.active', product_id)
        self.refresh_companies()
        if hasattr(self, 'market_start'):
            self.update_resume_labels()

    def refresh_companies(self, *_):
        if not hasattr(self, 'company_table'):
            return
        mode = self.company_filter.currentText()
        rows = visible_companies(self.store, self.product_id, include_rejected=mode == 'Не подходят',
                                 include_history=True) if self.product_id else []
        if mode == 'Не подходят':
            rows = [row for row in rows if row['fit_status'] == 'NOT_FIT']
        search = self.company_search.text().strip().casefold()
        mode = self.company_filter.currentText()
        self.company_rows = [row for row in rows if (not search or search in ' '.join(str(row[k] or '') for k in ('name', 'email', 'phone')).casefold()) and (mode != 'Подходят' or row['fit_status'] == 'FIT') and (mode != 'Нужна проверка' or row['fit_status'] == 'INSUFFICIENT_EVIDENCE') and (mode != 'Без УТП' or not row['offer_id']) and (mode != 'Отправлено' or row['sent']) and (mode != 'Ответили' or row['replied'])]
        for index in range(self.company_table.rowCount()):
            for column in (0, 2, 3, 5):
                previous = self.company_table.cellWidget(index, column)
                if previous is not None:
                    previous.hide()
                    self.company_table.removeCellWidget(index, column)
        self.company_table.setRowCount(len(self.company_rows))
        for index, row in enumerate(self.company_rows):
            company = CompanyLink(row['name'])
            company.clicked.connect(lambda checked=False, url=row['url']: open_site(url))
            company.setObjectName('tableLink')
            company.setCursor(Qt.CursorShape.PointingHandCursor)
            company.setEnabled(bool(row['url']))
            company.setToolTip(row['name'] + '\n' + (row['url'] or '') + '\nДвойной щелчок по строке — карточка компании')
            self.company_table.setCellWidget(index, 0, company)
            email_item = QTableWidgetItem(row['email'] or 'Email не найден')
            email_item.setToolTip(row['email'] or 'Email не найден')
            if not row['email']:
                email_item.setForeground(QColor(COLORS['muted']))
            self.company_table.setItem(index, 1, email_item)
            if row['phone']:
                phone = button(row['phone'], lambda checked=False, value=row['phone']: self.copy_phone(value))
                phone.setObjectName('tableLink')
                phone.setCursor(Qt.CursorShape.PointingHandCursor)
                phone.setToolTip('Скопировать телефон')
                cell_control(self.company_table, index, 2, phone)
            else:
                missing_phone = QTableWidgetItem('Телефон не найден')
                missing_phone.setForeground(QColor(COLORS['muted']))
                self.company_table.setItem(index, 2, missing_phone)
            if row['sent'] or row['uncertain']:
                status = 'Отправлено ✓' if row['sent'] else 'Проверяется'
                item = QTableWidgetItem(status)
                item.setForeground(QColor(COLORS['success'] if row['sent'] else COLORS['warning']))
                self.company_table.setItem(index, 3, item)
            elif row['email'] and row['fit_status'] == 'FIT':
                offer = button('УТП', lambda checked=False, company_id=row['id']: self.preview_offer(company_id))
                offer.setObjectName('cellAction')
                offer.setToolTip('Подготовить и посмотреть письмо перед отправкой')
                cell_control(self.company_table, index, 3, offer)
            else:
                status = 'Нужна проверка' if row['fit_status'] == 'INSUFFICIENT_EVIDENCE' else ('Не подходит' if row['fit_status'] == 'NOT_FIT' else 'Нет email')
                item = QTableWidgetItem(status)
                if row['fit_status'] == 'INSUFFICIENT_EVIDENCE':
                    item.setForeground(QColor(COLORS['warning']))
                else:
                    item.setForeground(QColor(COLORS['muted']))
                self.company_table.setItem(index, 3, item)
            reply = QTableWidgetItem('✓' if row['replied'] else '—')
            reply.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if row['replied']:
                reply.setForeground(QColor(COLORS['success']))
                reply.setToolTip('Получен ответ')
            else:
                reply.setForeground(QColor(COLORS['muted']))
                reply.setToolTip('Ответ пока не получен')
            self.company_table.setItem(index, 4, reply)
            history = button('Открыть', lambda checked=False, company_id=row['id']: self.show_history(company_id))
            history.setObjectName('tableLink')
            history.setCursor(Qt.CursorShape.PointingHandCursor)
            history.setToolTip('История переписки')
            cell_control(self.company_table, index, 5, history)
            self.company_table.setRowHeight(index, max(50, self.fontMetrics().height() + 28))
        self.company_empty.setVisible(not self.company_rows)
        self.company_empty.setText('Нажмите «Найти компании» или добавьте сайт через «Ещё».' if self.product_id else 'Выберите товар в «Рынке» или введите свой выше.')

    def copy_phone(self, value):
        QApplication.clipboard().setText(value)
        self.statusBar().showMessage('Телефон скопирован.', 3000)

    def company_details(self, row, column):
        if 0 <= row < len(self.company_rows):
            CompanyDialog(self.store, self.product_id, self.company_rows[row]['id'], self).exec()
            self.refresh_companies()

    def preview_offer(self, company_id):
        OfferDialog(self, company_id).exec()
        self.refresh_companies()

    def show_history(self, company_id):
        HistoryDialog(self.store, self.product_id, company_id, self).exec()

    def start_market(self):
        self.start_research('market')

    def start_companies(self):
        if self.product_id:
            signature = json.dumps(self.countries, ensure_ascii=False)
            run = self.store.one("SELECT id,kind FROM job_runs WHERE product_id=? AND countries=? AND kind='companies' AND status IN ('paused','failed','running','interrupted','complete_partial') ORDER BY id DESC LIMIT 1", (self.product_id, signature))
            self.start_research('companies', run_id=run['id'] if run else None,
                                manual_resume=bool(run and run['kind'].startswith('manual:')))
        else:
            self.guide('Сначала выберите товар в «Рынке» или введите свой товар.')

    def add_company(self):
        if not self.product_id:
            return
        dialog = QInputDialog(self)
        dialog.setWindowTitle('Добавить компанию')
        dialog.setLabelText('Ссылка на официальный сайт')
        dialog.setTextValue('https://')
        dialog.setOkButtonText('Добавить')
        dialog.setCancelButtonText('Отмена')
        dialog.resize(480, 140)
        if dialog.exec() and dialog.textValue().strip():
            self.start_research('companies', dialog.textValue().strip())

    def start_research(self, kind, manual_url=None, run_id=None, manual_resume=False, recovery=False):
        if self._foreground_worker is not None:
            return
        if self.search_check_busy:
            self.statusBar().showMessage('Дождитесь проверки поискового источника.', 8000)
            return
        if self.research_cancel is not None:
            self.statusBar().showMessage('Дождитесь завершения текущего поиска.', 8000)
            return
        if not self.countries:
            self.statusBar().showMessage('Выберите страны исследования.', 8000)
            return
        market_resume = kind == 'market' and bool(self.store.one(
            "SELECT id FROM job_runs WHERE kind='market' AND product_id IS NULL AND countries=? AND status IN ('paused','failed','running','interrupted','complete_partial') LIMIT 1",
            (json.dumps(self.countries, ensure_ascii=False),)))
        sources = (self.research.configured_sources(kind) if hasattr(self.research, 'configured_sources') else
                   self.research.available_sources(kind) if hasattr(self.research, 'available_sources') else None)
        if not manual_url and not manual_resume and not run_id and not market_resume and sources == []:
            diagnostic = getattr(self.research, 'no_sources_error', None)
            message = (str(diagnostic(kind)) if diagnostic else
                       'Нет доступных источников поиска. Проверьте подключения в настройках.')
            self.guide(message, settings=True)
            return
        if not recovery:
            # Once the user chooses work, old startup jobs must not take over
            # the visible product/countries after that work completes.
            self._auto_recovery_enabled = False
            self.recovery_timer.stop()
        self.clear_guide()
        (self.market_research_notice if kind == 'market' else self.company_research_notice).hide()
        if kind == 'market':
            self.clear_guide()
            self.market_research_notice.setText('Идёт исследование рынка…')
            self.market_research_notice.show()
        else:
            self.company_research_notice.setText('Поиск идёт. Ищем компании, которым может подойти товар.')
            self.company_research_notice.show()
        self.research_cancel = threading.Event()
        self.research_kind = kind
        token = self.research_cancel
        countries, product_id = list(self.countries), self.product_id
        self.market_start.setEnabled(False)
        self.company_start.setEnabled(False)
        self.manual_company.setEnabled(False)
        self.branches.setEnabled(False)
        self.country_button.setEnabled(False)
        for check in self.source_checks.values():
            check.setEnabled(False)
        def operation(emit):
            if kind == 'market':
                if run_id is not None and hasattr(self.research, 'resume_market'):
                    return self.research.resume_market(run_id, countries, emit, token)
                return self.research.research_market(countries, emit, token)
            if run_id is not None:
                return self.research.resume_companies(run_id, product_id, countries, emit, token)
            return self.research.find_companies(product_id, countries, emit, token, manual_url=manual_url)
        def failed(message):
            self.store.audit('research.failed', str(message)[:1000])
            if kind == 'market':
                self.market_research_notice.setText('Не удалось закончить поиск. Найденные результаты сохранены.')
                self.market_research_notice.show()
                self.guide(message, tone='error')
            else:
                self.guide(message, tone='error')
        message = ('Исследуем рынок. Ищем востребованные товары' if kind == 'market' else
                   'Изучаем сайты и собираем контакты' if manual_url or manual_resume else
                   'Ищем компании, которым может подойти товар')
        self.submit(operation, self.research_done, finished=self.research_finished, failed=failed,
                    busy_message=message, cancel=self.pause_research)

    def pause_research(self):
        if self.research_cancel is not None:
            self.research_cancel.set()
            self.market_pause.setEnabled(False)
            self.company_pause.setEnabled(False)
            self.market_pause.setText('Сохраняем…')
            self.company_pause.setText('Сохраняем…')
            self.busy_overlay.cancel_button.setText('Сохраняем…')
            self.busy_overlay.cancel_button.setEnabled(False)
            self.statusBar().showMessage('Сохраняем прогресс…')

    def research_done(self, result):
        if isinstance(result, dict):
            if self.research_kind == 'market':
                self.clear_guide()
                message = result.get('message')
                if result.get('run_id'):
                    message = market_summary(self.store, result['run_id'], self.countries)['message']
                self.market_research_notice.setText(message or 'Исследование закончено. Результаты сохранены.')
                self.market_research_notice.show()
                return
            status = result.get('status')
            count = result.get('count', len(self.company_rows))
            exhausted = 'Доступные источники исчерпаны. ' if result.get('reason') == 'sources_exhausted' else ''
            message = (f'Поиск завершён. {exhausted}Найдено компаний: {count}.' if status in ('complete', 'completed', 'complete_partial') else
                       'Не удалось закончить поиск. Найденные результаты сохранены.')
            if 'sources_used' in result and 'sources_total' in result and status in ('complete', 'completed', 'complete_partial'):
                message += f" Использовано источников: {result['sources_used']}/{result['sources_total']}."
            self.clear_guide()
            self.company_research_notice.setText(message)
            self.company_research_notice.show()

    def research_finished(self):
        self.research_cancel = None
        if self._closing:
            return
        self.market_start.setEnabled(True)
        self.company_start.setEnabled(bool(self.product_id))
        self.manual_company.setEnabled(bool(self.product_id))
        self.branches.setEnabled(True)
        self.country_button.setEnabled(True)
        for check in self.source_checks.values():
            check.setEnabled(True)
        self.market_pause.setEnabled(False)
        self.company_pause.setEnabled(False)
        self.market_pause.hide()
        self.company_pause.hide()
        self.market_start.show()
        self.company_start.show()
        self.refresh_market()
        self.refresh_companies()
        self.update_resume_labels()
        self.update_search_usage()
        if self._auto_recovery_enabled and not self.recovery_timer.isActive():
            self.recovery_timer.start(100)

    def update_resume_labels(self):
        self.market_start.setText('Найти товары')
        self.company_start.setText('Найти компании')
        self.restore_research_notices()

    def restore_research_notices(self):
        signature = json.dumps(self.countries, ensure_ascii=False)
        for kind, product_id, widget in (('market', None, self.market_research_notice),
                                         ('companies', self.product_id, self.company_research_notice)):
            if kind == 'companies':
                row = self.store.one("SELECT status,message FROM job_runs WHERE (kind='companies' OR kind LIKE 'manual:%') AND product_id IS ? AND countries=? AND status IN ('paused','failed','running','interrupted','complete_partial') ORDER BY id DESC LIMIT 1", (product_id, signature))
            else:
                row = self.store.one('SELECT status,message FROM job_runs WHERE kind=? AND product_id IS ? AND countries=? ORDER BY id DESC LIMIT 1', (kind, product_id, signature))
            if kind == 'market' and row:
                latest = self.store.one("SELECT id FROM job_runs WHERE kind='market' AND product_id IS NULL AND countries=? ORDER BY id DESC LIMIT 1", (signature,))
                text = market_summary(self.store, latest['id'], self.countries)['message']
            else:
                if row and row['status'] in ('paused', 'failed', 'running', 'interrupted', 'complete_partial'):
                    text = ('Поиск идёт. Ищем компании, которым может подойти товар.' if self.research_cancel is not None else
                            'Незавершённый поиск сохранён для автоматического восстановления.')
                else:
                    text = ''
                if not row and widget.text().startswith('Поиск завершён.'):
                    continue
            widget.setText(text)
            widget.setVisible(bool(text))

    def refresh_research(self):
        if self._closing:
            return
        if self.research_cancel is not None:
            self.refresh_market()
            self.refresh_companies()
            self.restore_research_notices()

    def recover_research(self):
        """Recover saved work one run at a time, with no user queue controls."""
        if self._closing or not self._auto_recovery_enabled or not hasattr(self.research, 'research_market'):
            return
        if self._foreground_worker is not None or self.search_check_busy or self.mail_busy:
            self.recovery_timer.start(1000)
            return
        runs = self.store.query("""SELECT * FROM job_runs WHERE status IN
            ('paused','failed','running','interrupted','complete_partial')
            AND EXISTS(SELECT 1 FROM job_queue q WHERE q.run_id=job_runs.id AND q.status!='done')
            ORDER BY id""")
        for run in runs:
            if run['id'] in self._recovery_attempted:
                continue
            self._recovery_attempted.add(run['id'])
            try:
                countries = json.loads(run['countries'])
            except (ValueError, TypeError):
                continue
            if not countries or not isinstance(countries, list):
                continue
            self.countries = countries
            self.country_button.setText(self.country_text())
            if run['kind'] == 'market':
                self.tabs.setCurrentIndex(0)
                self.start_research('market', run_id=run['id'], recovery=True)
            elif run['product_id']:
                self.open_product(run['product_id'])
                self.start_research('companies', run_id=run['id'], manual_resume=run['kind'].startswith('manual:'), recovery=True)
            return

    def build_settings(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 12, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        content.setObjectName('settingsContent')
        grid = QGridLayout(content)
        grid.setContentsMargins(0, 0, 12, 6)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        ai_group = QGroupBox('GigaChat')
        ai_group.setObjectName('aiCard')
        form = QFormLayout(ai_group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.giga_key = field('Ключ авторизации', True)
        self.giga_key.setText(self.secrets.get('gigachat_key'))
        self.ai_scope = QComboBox()
        self.ai_scope.addItems(['GIGACHAT_API_PERS', 'GIGACHAT_API_B2B', 'GIGACHAT_API_CORP'])
        self.ai_scope.setCurrentText(self.store.get_setting('ai.scope', 'GIGACHAT_API_PERS'))
        self.ai_model = QComboBox()
        models = list(dict.fromkeys(['GigaChat-2'] + self.store.get_setting('ai.models', [])))
        self.ai_model.addItems(models)
        self.ai_model.setCurrentText(self.store.get_setting('ai.model', 'GigaChat-2'))
        self.ai_limit = spin(1000, 1_000_000, self.store.get_setting('ai.token_limit', 30000))
        self.ai_status = label('Не подключено')
        self.ai_status.setObjectName('muted')
        self.ai_test = button('Проверить', self.test_ai)
        self.ai_reconcile = button('Сверить расход', self.reconcile_ai_usage)
        for title, widget in [('Ключ', self.giga_key), ('Доступ', self.ai_scope), ('Модель', self.ai_model), ('Лимит токенов', self.ai_limit)]:
            form.addRow(title, widget)
        form.addRow(self.ai_status)
        form.addRow(self.ai_test)
        form.addRow(self.ai_reconcile)
        grid.addWidget(ai_group, 0, 0)

        sender_group = QGroupBox('Компания-отправитель')
        sender_group.setObjectName('senderCard')
        form = QFormLayout(sender_group)
        self.sender_name = field('Компания')
        self.sender_name.setText(self.store.get_setting('sender.name', 'Аргон'))
        self.sender_signature = field('Имя, должность, контакты')
        self.sender_signature.setText(self.store.get_setting('sender.signature', ''))
        form.addRow('Название', self.sender_name)
        form.addRow('Подпись', self.sender_signature)

        mail_group = QGroupBox('Почта')
        mail_group.setObjectName('mailCard')
        form = QFormLayout(mail_group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        account = self.store.one('SELECT * FROM mail_accounts WHERE id=1') or {}
        self.mail_provider = QComboBox()
        for key, title in [('gmail', 'Gmail (Google)'), ('yandex', 'Яндекс.Почта'), ('manual', 'Вручную (IMAP/SMTP)')]:
            self.mail_provider.addItem(title, key)
        self.mail_provider.setCurrentIndex(max(0, self.mail_provider.findData(account.get('provider', 'gmail'))))
        provider = self.mail_provider.currentData()
        host = 'gmail.com' if provider == 'gmail' else 'yandex.ru'
        self.mail_email = field('name@gmail.com' if provider == 'gmail' else 'name@yandex.ru')
        self.mail_email.setText(account.get('email', ''))
        self.mail_password = field('Отдельный пароль приложения', True)
        self.mail_password.setText(self.secrets.get('mail_password'))
        self.imap_host = field('imap.' + host)
        self.imap_host.setText(account.get('imap_host', 'imap.' + host))
        self.imap_port = spin(1, 65535, account.get('imap_port', 993))
        self.smtp_host = field('smtp.' + host)
        self.smtp_host.setText(account.get('smtp_host', 'smtp.' + host))
        self.smtp_port = QComboBox()
        self.smtp_port.addItems(['465', '587'])
        self.smtp_port.setCurrentText(str(account.get('smtp_port', 465)))
        form.addRow('Провайдер', self.mail_provider)
        form.addRow('Email', self.mail_email)
        form.addRow('Пароль', self.mail_password)
        self.mail_advanced = QWidget()
        advanced = QFormLayout(self.mail_advanced)
        advanced.setContentsMargins(0, 0, 0, 0)
        for title, widget in [('IMAP', self.imap_host), ('Порт IMAP', self.imap_port), ('SMTP', self.smtp_host), ('Порт SMTP', self.smtp_port)]:
            advanced.addRow(title, widget)
        form.addRow(self.mail_advanced)
        self.mail_help = label('')
        self.mail_help.setObjectName('muted')
        form.addRow(self.mail_help)
        self.mail_help_toggle = QPushButton('Если пароль недоступен ▸')
        self.mail_help_toggle.setObjectName('textLink')
        self.mail_help_toggle.setCheckable(True)
        self.mail_help_detail = label('')
        self.mail_help_detail.setObjectName('muted')
        self.mail_help_toggle.toggled.connect(lambda expanded: self.mail_help_detail.setVisible(expanded))
        form.addRow(self.mail_help_toggle)
        form.addRow(self.mail_help_detail)
        self.mail_password_link = button('Создать пароль Google', lambda: open_site('https://myaccount.google.com/apppasswords'))
        self.mail_password_link.setObjectName('textLink')
        form.addRow(self.mail_password_link)
        self.mail_provider.currentIndexChanged.connect(self.mail_provider_changed)
        self.mail_provider_changed()
        self.mail_status = label('Подключено' if account.get('connected') else 'Не подключено')
        self.mail_status.setObjectName('muted')
        form.addRow(self.mail_status)
        row = QHBoxLayout()
        self.mail_test = button('Подключить', self.test_mail)
        self.mail_disconnect = button('Отключить', self.disconnect_mail)
        self.mail_disconnect.setVisible(bool(account.get('connected')))
        row.addWidget(self.mail_test)
        row.addWidget(self.mail_disconnect)
        form.addRow(row)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(sender_group)
        right_layout.addWidget(mail_group)
        right_layout.addStretch()
        grid.addWidget(right, 0, 1, 1, 1)
        grid.setAlignment(ai_group, Qt.AlignmentFlag.AlignTop)

        search_group = QGroupBox('Поисковые источники')
        search_group.setObjectName('searchCard')
        search_layout = QVBoxLayout(search_group)
        search_layout.addWidget(label('Доступен поиск без ключей. Для API-источников бесплатный доступ проверяется отдельно; Tavily Researcher поддерживает бесплатную квоту.'))
        self.source_selector = QComboBox()
        self.source_selector.setObjectName('sourceSelector')
        self.source_stack = QStackedWidget()
        self.source_selector.currentIndexChanged.connect(self.source_stack.setCurrentIndex)
        search_layout.addWidget(self.source_selector)
        search_layout.addWidget(self.source_stack)
        self.source_enabled = {}
        self.source_keys = {}
        self.source_status = {}
        self.source_usage = {}
        self.source_checks = {}
        for name, title in {**KEYLESS_SOURCES, **KEYED_SOURCES}.items():
            keyed = name in KEYED_SOURCES
            card = QGroupBox(title + (' · API' if keyed else ' · без ключа'))
            card.setObjectName('sourceCard')
            card_grid = QGridLayout(card)
            card_grid.setColumnStretch(1, 1)
            enabled = QCheckBox('Использовать')
            enabled.setChecked(self.store.get_setting('search.' + name + '_enabled', (not keyed and name not in ('catalog', 'procurement')) or name == 'firecrawl'))
            enabled.setToolTip('Firecrawl может работать без ключа в доступной бесплатной квоте.' if name == 'firecrawl' else 'Включить источник для поиска')
            self.source_enabled[name] = enabled
            card_grid.addWidget(enabled, 0, 0)
            if keyed:
                key_input = field('Необязательный ключ' if name == 'firecrawl' else 'API-ключ', True)
                key_input.setText(self.secrets.get(name + '_key'))
                self.source_keys[name] = key_input
                card_grid.addWidget(key_input, 0, 1)
            else:
                card_grid.addWidget(label('Регистрация не нужна'), 0, 1)
            check = button('Проверить', lambda checked=False, source=name: self.test_search(source))
            self.source_checks[name] = check
            card_grid.addWidget(check, 0, 2)
            status = label('Не проверен')
            status.setObjectName('muted')
            self.source_status[name] = status
            card_grid.addWidget(status, 1, 0, 1, 3)
            usage = label('Учтено запросов: 0 · Остаток квоты: неизвестен')
            usage.setObjectName('muted')
            self.source_usage[name] = usage
            card_grid.addWidget(usage, 2, 0, 1, 3)
            self.source_stack.addWidget(card)
            self.source_selector.addItem(title + (' · API' if keyed else ' · без ключа'), name)
        # Retain these attributes for existing integrations; all controls are per-source.
        self.exa_key, self.fire_key = self.source_keys['exa'], self.source_keys['firecrawl']
        self.exa_enabled, self.fire_enabled = self.source_enabled['exa'], self.source_enabled['firecrawl']
        self.search_test = self.source_checks['firecrawl']
        # Retained only for compatibility with existing integrations/settings.
        # A checkbox cannot prove billing is disabled and is no longer offered.
        self.free_confirmed = QCheckBox(self)
        self.free_confirmed.setChecked(self.store.get_setting('search.free_confirmed', False))
        self.free_confirmed.hide()
        note = label('Проверка API подтверждает бесплатный режим отдельно от действительности ключа. Если безопасность списаний не подтверждена, используются другие источники.')
        note.setObjectName('muted')
        search_layout.addWidget(note)
        self.request_limit = spin(10, 1000, self.store.get_setting('search.request_limit', 100))
        self.page_limit = spin(2, 20, self.store.get_setting('search.page_limit', 8))
        limits = QFormLayout()
        limits.addRow('Запросов в пакете', self.request_limit)
        limits.addRow('Страниц на компанию', self.page_limit)
        search_layout.addLayout(limits)
        self.extra_source_fields = {}
        for setting, title in [('rss_urls', 'RSS-ленты'), ('catalog_urls', 'Каталоги'), ('procurement_urls', 'Публикации закупок')]:
            row = QFormLayout()
            item = field('URL через пробел; только разрешённые публичные страницы')
            urls = self.store.get_setting('search.' + setting, None if setting == 'rss_urls' else [])
            item.setText(' '.join(RSSProvider.effective_urls(urls) if setting == 'rss_urls' else urls))
            item.setToolTip('Собственные источники. Указывайте только адреса, разрешённые для автоматического чтения.')
            if setting == 'procurement_urls':
                item.setPlaceholderText('Прямые URL разрешённых публикаций через пробел')
                item.setToolTip('На вкладке «Рынок» читаются только указанные страницы. '
                                'Вводите прямые ссылки на публичные закупки, разрешённые для автоматического чтения.')
            self.extra_source_fields[setting] = item
            row.addRow(title, item)
            search_layout.addLayout(row)
        self.search_status = label('')
        self.search_status.setObjectName('muted')
        search_layout.addWidget(self.search_status)
        grid.addWidget(search_group, 1, 0, 1, 2)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        footer = QHBoxLayout()
        footer_note = label('Ключи защищены хранилищем Windows.')
        if getattr(self.secrets, 'warnings', []):
            warning = label('\n'.join(self.secrets.warnings))
            warning.setObjectName('warningNotice')
            layout.addWidget(warning)
        footer_note.setWordWrap(False)
        footer.addWidget(footer_note)
        footer.addStretch()
        self.save_settings_button = button('Сохранить', self.save_settings, True)
        footer.addWidget(self.save_settings_button)
        layout.addLayout(footer)
        self.tabs.addTab(page, 'Настройки')
        self.search_usage_timer = QTimer(self)
        self.search_usage_timer.setSingleShot(True)
        self.search_usage_timer.setInterval(250)
        self.search_usage_timer.timeout.connect(self.update_search_usage)
        for key_input in self.source_keys.values():
            key_input.textChanged.connect(lambda *_: self.search_usage_timer.start())
        self.update_ai_status()
        self.update_search_usage()

    def mail_provider_changed(self, *_):
        from sales_manager.mail import provider_help
        provider = self.mail_provider.currentData()
        self.mail_advanced.setVisible(provider == 'manual')
        self.mail_help.setText('Полный адрес Gmail и отдельный пароль приложения Google. Нужна двухэтапная проверка.' if provider == 'gmail' else provider_help(provider))
        self.mail_help_detail.setText(provider_help(provider))
        self.mail_help_toggle.setChecked(False)
        self.mail_help_detail.hide()
        self.mail_help_toggle.setVisible(provider == 'gmail')
        self.mail_password_link.setVisible(provider == 'gmail')
        self.mail_email.setPlaceholderText('name@gmail.com' if provider == 'gmail' else 'name@yandex.ru' if provider == 'yandex' else 'Полный email')

    def _mail_values(self, email, old):
        from sales_manager.mail.service import address, provider_settings

        if address(email) != email:
            raise ValueError('Укажите один корректный email отправителя.')
        provider = self.mail_provider.currentData()
        if provider == 'manual':
            imap_host, smtp_host = self.imap_host.text().strip(), self.smtp_host.text().strip()
            imap_port, smtp_port = self.imap_port.value(), int(self.smtp_port.currentText())
        else:
            settings = old if old.get('provider') == provider else provider_settings(provider)
            imap_host, imap_port, smtp_host, smtp_port = (settings[key] for key in
                ('imap_host', 'imap_port', 'smtp_host', 'smtp_port'))
        if any(not re.fullmatch(r'[A-Za-z0-9.-]+', host or '') for host in (imap_host, smtp_host)):
            raise ValueError('Проверьте адреса почтовых серверов.')
        if int(imap_port) != 993 or int(smtp_port) not in (465, 587):
            raise ValueError('Поддерживаются IMAP 993 и SMTP 465/587 с TLS.')
        return email, provider, imap_host, imap_port, smtp_host, smtp_port

    def save_settings(self, *_, verified_mail=None):
        try:
            from sales_manager.mail.service import provider_settings

            # Phase 1: build and validate EVERYTHING, including the mailbox,
            # before changing a single database row or credential.
            email = self.mail_email.text().strip().casefold()
            if self.ai_scope.currentText() not in ('GIGACHAT_API_PERS', 'GIGACHAT_API_B2B', 'GIGACHAT_API_CORP'):
                raise ValueError('Выберите корректный доступ GigaChat.')
            settings = {
                'sender.name': self.sender_name.text().strip() or 'Аргон',
                'sender.signature': self.sender_signature.text().strip(),
                'ai.scope': self.ai_scope.currentText(), 'ai.model': self.ai_model.currentText(),
                'ai.token_limit': self.ai_limit.value(),
                'search.exa_enabled': self.exa_enabled.isChecked(),
                'search.firecrawl_enabled': self.fire_enabled.isChecked(),
                'search.free_confirmed': self.free_confirmed.isChecked(),
                'search.request_limit': self.request_limit.value(),
                'search.page_limit': self.page_limit.value(),
            }
            settings.update({'search.' + name + '_enabled': widget.isChecked()
                             for name, widget in self.source_enabled.items()})
            settings.update(validate_extra_urls({setting: widget.text().split()
                                                 for setting, widget in self.extra_source_fields.items()}))
            credentials = {'gigachat_key': self.giga_key.text().strip()}
            credentials.update({name + '_key': widget.text().strip()
                                for name, widget in self.source_keys.items()})
            ai_changed = (credentials['gigachat_key'] != self.secrets.get('gigachat_key') or
                          settings['ai.scope'] != self.store.get_setting('ai.scope', 'GIGACHAT_API_PERS'))
            if ai_changed:
                # A generation changes even if a user switches back to the old key:
                # replies from either previous connection must not overwrite it.
                settings.update({'ai.connection_generation': uuid.uuid4().hex,
                                 'ai.balance': None, 'ai.checked_at': '', 'ai.models': []})
            mail_values = None
            mail_password = None
            sent_folder = None
            old = self.store.one('SELECT * FROM mail_accounts WHERE id=1') or {}
            if email:
                values = self._mail_values(email, old)
                mail_password = self.mail_password.text()
                changed = (values != tuple(old.get(key) for key in
                           ('email', 'provider', 'imap_host', 'imap_port', 'smtp_host', 'smtp_port'))
                           or self.secrets.get('mail_password') != mail_password)
                if verified_mail is not None:
                    if verified_mail[:2] != (values, mail_password):
                        raise ValueError('Настройки почты изменились во время проверки. Подключите почту ещё раз.')
                    mail_values, sent_folder = values, verified_mail[2]
                elif changed:
                    if self.mail_busy:
                        raise ValueError('Дождитесь завершения работы с почтой перед сменой подключения.')
                    mail_values = values
            elif verified_mail is not None:
                raise ValueError('Настройки почты изменились во время проверки. Подключите почту ещё раз.')
            elif old.get('email') or old.get('connected') or self.secrets.get('mail_password'):
                if self.mail_busy:
                    raise ValueError('Дождитесь завершения работы с почтой перед сменой подключения.')
                # Retain mailbox configuration and all message history while
                # explicitly disabling the account and removing its password.
                defaults = provider_settings(self.mail_provider.currentData())
                mail_values = ('', old.get('provider') or self.mail_provider.currentData(),
                               old.get('imap_host') or defaults['imap_host'], old.get('imap_port') or defaults['imap_port'],
                               old.get('smtp_host') or defaults['smtp_host'], old.get('smtp_port') or defaults['smtp_port'])
                mail_password = ''
            # Phase 2: one SQLite transaction and compensating credential restore.
            apply_settings(self.store, self.secrets, settings, credentials,
                           mail_values, mail_password, sent_folder=sent_folder)
            if mail_values is not None:
                self.mail_status.setText('Подключено' if sent_folder is not None else 'Не подключено')
                self.mail_disconnect.setVisible(sent_folder is not None)
                if not email:
                    self.mail_password.clear()
            if ai_changed:
                self.update_ai_status()
            self.statusBar().showMessage('Настройки сохранены.', 5000)
            self.update_search_usage()
            return True
        except Exception as exc:
            QMessageBox.information(self, 'Настройки', str(exc))
            return False

    def reconcile_ai_usage(self):
        estimate = max(0, int(self.store.get_setting('ai.unconfirmed_tokens', 0) or 0))
        if not estimate:
            QMessageBox.information(self, 'Сверка расхода', 'Неподтверждённого расхода нет.')
            return
        actual, accepted = QInputDialog.getInt(
            self, 'Сверка расхода',
            f'Оценка неподтверждённого расхода: {estimate} токенов.\n'
            'Проверьте у GigaChat и укажите общий фактический расход именно по этим запросам. '
            'Не включайте уже подтверждённый расход:', 0, 0, 2147483647)
        if not accepted:
            return
        if QMessageBox.question(self, 'Подтверждение сверки',
                                f'Заменить оценку {estimate} на проверенный расход {actual} токенов?') != QMessageBox.StandardButton.Yes:
            return
        try:
            self.ai.reconcile_uncertain_usage(actual, expected_estimate=estimate)
            self.update_ai_status()
        except Exception as exc:
            QMessageBox.information(self, 'Сверка расхода', str(exc))

    def update_ai_status(self):
        usage = self.store.one("SELECT COALESCE(SUM(input_tokens+output_tokens),0) AS total FROM usage_log WHERE provider='gigachat'")['total']
        balance = self.store.get_setting('ai.balance')
        checked = self.store.get_setting('ai.checked_at', '')
        if not checked:
            text = 'Не подключено'
        elif isinstance(balance, dict) and isinstance(balance.get('balance'), list):
            balances = balance.get('balance') or balance.get('balances') or []
            if isinstance(balances, list):
                detail = ', '.join(f"{r.get('value', r.get('usage', '—'))} токенов" for r in balances if isinstance(r, dict))
            else:
                detail = str(balances)
            text = ('Остаток: ' + detail if detail else 'Баланс получен') + ' · ' + str(checked)[:16]
        else:
            text = 'Баланс по API недоступен'
        incomplete = self.store.get_setting('ai.usage_incomplete', False)
        unconfirmed = max(0, int(self.store.get_setting('ai.unconfirmed_tokens', 0) or 0))
        uncertain = int(self.store.get_setting('ai.uncertain_requests', 0) or 0)
        tail = (f'\nНеподтверждённый расход (оценка, включая обрывы связи): {unconfirmed} токенов'
                + (f'; потерянных ответов: {uncertain}' if uncertain else '')
                if incomplete else '')
        self.ai_status.setText(f'{text}\nПодтверждённый расход: {usage} токенов{tail}')
        self.ai_status.setToolTip(json.dumps(balance, ensure_ascii=False) if balance else '')
        model = self.ai_model.currentText()
        models = self.store.get_setting('ai.models', [])
        if models:
            self.ai_model.clear()
            self.ai_model.addItems(models)
            if model not in models:
                model = self.store.get_setting('ai.model', 'GigaChat-2')
            index = self.ai_model.findText(model)
            if index >= 0:
                self.ai_model.setCurrentIndex(index)

    def update_search_usage(self):
        self.search_usage_timer.stop()
        rows = self.store.query("SELECT provider,COUNT(*) AS calls FROM usage_log WHERE provider<>'gigachat' GROUP BY provider")
        counts = {row['provider']: row['calls'] for row in rows}
        # The fields already hold the current credentials. A key edit must not
        # synchronously reread WinVault or inherit verification of the old key.
        keys = {name: control.text().strip() for name, control in self.source_keys.items()}
        bulk_status = hasattr(self.research, 'provider_statuses')
        states = self.research.provider_statuses(self.source_status, keys=keys) if bulk_status else {}
        for name, widget in self.source_status.items():
            state = states.get(name, {}) if bulk_status else (
                self.research.provider_status(name) if hasattr(self.research, 'provider_status') else {})
            blocked = state.get('status') == 'Отключён: оплата не проверена'
            if blocked:
                text = 'Недоступен: оплата не проверена'
            elif not self.source_enabled[name].isChecked():
                text = 'Отключён'
            else:
                text = state.get('status') or 'Не проверен'
            widget.setText(text)
            widget.setToolTip(state.get('message', 'Нажмите «Проверить» для проверки источника и бесплатного доступа.'))
            self.source_checks[name].setEnabled(not self.search_check_busy and self.research_cancel is None)
            self.source_checks[name].setToolTip('Проверить подключение и возможность бесплатного использования')
            quota = state.get('quota')
            balance = str(quota) if quota is not None else 'неизвестен'
            self.source_usage[name].setText(f'Учтено запросов: {counts.get(name, 0)} · Остаток квоты: {balance}')
        mode = (self.research.search_mode(keys=keys) if bulk_status else self.research.search_mode()) if hasattr(self.research, 'search_mode') else 'Режим поиска: базовый — без API-ключей'
        self.search_mode_label.setText(mode)
        self.search_status.setText('Учтены запросы этого приложения. Остаток доступен только если его сообщил провайдер.')
        self.search_status.setToolTip('Локальный счётчик не подтверждает отсутствие оплаты у провайдера.')

    def test_ai(self):
        if self._foreground_worker is not None:
            return
        if not self.giga_key.text().strip():
            self.ai_status.setText('Нет ключа. Введите ключ авторизации GigaChat и нажмите «Проверить».')
            self.guide('Введите ключ GigaChat в настройках и нажмите «Проверить».', settings=True)
            return
        if not self.save_settings():
            return
        self.ai_test.setEnabled(False)
        generation = self.store.get_setting('ai.connection_generation', '')
        self.ai_status.setText('Проверяем подключение GigaChat…')
        def failed(message):
            if generation != self.store.get_setting('ai.connection_generation', ''):
                return
            self.ai_status.setText(message)
            self.guide(message, settings=True, tone='error')
        def succeeded(result):
            if generation == self.store.get_setting('ai.connection_generation', ''):
                self.update_ai_status()
        self.submit(lambda emit: self.ai.test(expected_generation=generation), succeeded, failed=failed,
                    finished=lambda: self.ai_test.setEnabled(True), busy_message='Проверяем подключение GigaChat')

    def test_search(self, name=None):
        if self._foreground_worker is not None:
            return
        if self.research_cancel is not None or self.search_check_busy:
            self.guide('Дождитесь завершения текущего поиска.')
            return
        name = name if isinstance(name, str) else 'firecrawl'
        if name in self.source_keys and name != 'firecrawl' and not self.source_keys[name].text().strip():
            self.guide('Введите API-ключ выбранного источника в настройках.', settings=True)
            return
        if not self.save_settings():
            return
        if name in ('catalog', 'procurement') and not self.extra_source_fields[name + '_urls'].text().strip():
            self.guide('Сначала добавьте разрешённые публичные URL в поле «Каталоги» или «Публикации закупок» ниже.', settings=True)
            return
        self.search_check_busy = True
        for check in self.source_checks.values():
            check.setEnabled(False)
        self.source_status[name].setText('Проверяем источник…')
        def done(result):
            self.update_search_usage()
            self.guide(str(result))
        def failed(message):
            self.update_search_usage()
            self.source_status[name].setToolTip(message)
            self.guide(message, settings=True, tone='error')
        def finished():
            self.search_check_busy = False
            if not self._closing:
                for check in self.source_checks.values():
                    check.setEnabled(True)
                self.update_search_usage()
        self.submit(lambda emit: self.research.test_provider(name), done, failed=failed, finished=finished,
                    busy_message='Проверяем поисковый источник')

    def test_mail(self):
        if self._foreground_worker is not None:
            return
        if self.mail_busy:
            self.guide('Подключение почты уже проверяется. Дождитесь результата.')
            return
        email, password = self.mail_email.text().strip().casefold(), self.mail_password.text()
        if not email or not password.strip():
            self.mail_status.setText('Введите полный email и отдельный пароль приложения, затем нажмите «Подключить».')
            self.guide(self.mail_status.text(), settings=True)
            return
        try:
            values = self._mail_values(email, self.store.one('SELECT * FROM mail_accounts WHERE id=1') or {})
        except ValueError as exc:
            self.mail_error(str(exc))
            return
        self.mail_busy = True
        self.mail_test.setEnabled(False)
        self.mail_status.setText('Проверяем…')
        def verify(emit):
            return {'sent_folder': self.mail.verify_connection(values, password)}

        def connected(result):
            if not self.save_settings(verified_mail=(values, password, result['sent_folder'])):
                self.mail_status.setText('Настройки почты не сохранены.')

        self.submit(verify, connected,
                    failed=self.mail_error, finished=self.mail_finished, busy_message='Подключаем почту')

    def mail_error(self, message):
        self.mail_status.setText(message)
        self.guide(message, settings=True, tone='error')

    def mail_finished(self):
        self.mail_busy = False
        if not self._closing:
            self.mail_test.setEnabled(True)
            self.refresh_mail_button.setEnabled(True)
            account = self.store.one('SELECT connected FROM mail_accounts WHERE id=1') or {}
            self.mail_disconnect.setVisible(bool(account.get('connected')))
            self.refresh_companies()

    def sync_mail(self, manual=False):
        if self._foreground_worker is not None:
            return
        if self._closing or self.mail_busy:
            if manual and not self._closing:
                self.guide('Проверка почты уже выполняется. Дождитесь результата.')
            return
        account = self.store.one('SELECT connected FROM mail_accounts WHERE id=1')
        if not account or not account['connected']:
            if manual:
                self.guide('Чтобы проверить ответы, подключите почту в настройках: email, пароль приложения → «Подключить».', settings=True)
            return
        self.mail_busy = True
        self.refresh_mail_button.setEnabled(False)
        def sync(emit):
            return self.mail.sync()
        self.submit(sync, failed=self.mail_error if manual else lambda message: self.mail_status.setText(message),
                    finished=self.mail_finished, busy_message='Проверяем ответы' if manual else None, silent=not manual)

    def disconnect_mail(self):
        if self._foreground_worker is not None:
            return
        if self.mail_busy:
            self.statusBar().showMessage('Дождитесь завершения работы с почтой.', 8000)
            return
        self.mail_busy = True
        def done(result):
            self.mail_password.clear()
            self.mail_status.setText('Не подключено')
        self.submit(lambda emit: self.mail.disconnect(), done, finished=self.mail_finished)

    def closeEvent(self, event):
        self._closing = True
        self.startup_mail_timer.stop()
        self.recovery_timer.stop()
        if self.workers:
            self.timer.stop()
            self.refresh_timer.stop()
            if self.research_cancel is not None:
                self.research_cancel.set()
            self.centralWidget().setEnabled(False)
            self.action_notice.setText('Завершаем текущую операцию…')
            self.action_row.show()
            if self.busy_overlay.isVisible():
                self.busy_overlay.message.setText('Завершаем текущую операцию…')
            self.statusBar().showMessage('Завершаем текущую операцию…')
            self.close_timer.start()
            event.ignore()
        else:
            self.timer.stop()
            self.refresh_timer.stop()
            self.close_timer.stop()
            self.store.close()
            event.accept()

    def _finish_close(self):
        if not self.workers and self.pool.activeThreadCount() == 0:
            self.close()


def run():
    application = QApplication.instance() or QApplication(sys.argv)
    configure_application(application)
    root = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'Argon' / 'SalesManagerFirst'
    root.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(root / 'application.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None, 'SALES MANAGER FIRST', 'Приложение уже открыто.')
        return 0
    try:
        window = Window()
    except Exception:
        QMessageBox.critical(None, 'Не удалось открыть приложение', 'Не удалось открыть локальные данные или защищённое хранилище Windows.')
        lock.unlock()
        return 1
    window.show()
    result = application.exec()
    lock.unlock()
    return result
