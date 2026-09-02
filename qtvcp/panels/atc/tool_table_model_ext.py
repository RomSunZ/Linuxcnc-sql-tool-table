#!/usr/bin/env python3
"""
tool_table_model_ext.py — расширение стандартной таблицы инструментов qtvcp
под работу с tool_db_daemon.py, БЕЗ патчей классов фреймворка:

  - ExtToolTableModel(MyTableModel) — настоящий подкласс с 4 колонками
	(Diameter/Heavy/Big/Runtime), подставляется в КОНКРЕТНЫЙ экземпляр
	виджета tooloffsetview через setModel().
  - patch_tool_singleton_for_db_program() — переопределяет _reload/_save/
	periodic_check не на классе _TStat, а на КОНКРЕТНОМ singleton-объекте
	Tool() (qtvcp.core.Tool — честный __new__-синглтон, проверено по
	исходнику: Tool() из любого модуля возвращает один и тот же объект).
  - bind_ext_show_selection() — то же самое для showSelection, но на
	конкретном экземпляре виджета ToolOffsetView, а не на классе.
  - bind_ext_add_tool() — то же самое для add_tool(): вместо дублирования
	параметров текущей строки создаёт новый инструмент с номером
	max(существующие)+1 и обнулёнными полями.

ВАЖНО про запись таблицы инструментов (см. _save):
  - в этой конфигурации LinuxCNC получает данные инструментов НЕ из
	tool.tbl и НЕ через G10 L1 по MDI, а через [EMCIO]DB_PROGRAM =
	tool_db_daemon.py (интерфейс DB_PROGRAM v2.1, см. докстринг того
	файла) — LinuxCNC сам спрашивает у демона нужный инструмент по
	протоколу g/p/u. Отдельного файла таблицы инструментов в этой
	схеме нет вовсе, и писать/перечитывать его тут не нужно.
  - раньше здесь построчно слались G10 L1 через ACTION.CALL_MDI — это
	было неверно вдвойне: слово T там не используется, а P означает
	НОМЕР ИНСТРУМЕНТА (не pocket), да ещё и такой блок разбирает
	RS274NGC-парсер, который на станке без осей A/B/C/U/V/W валился
	с "Bad character used" на первой же недостающей оси.
  - правильный путь — то же, что уже используется для R/H/L/S: просто
	вызвать db_client.update_tool(tno, fields)/delete_tool(tno). Демон
	сам пишет в БД и сам уведомляет LinuxCNC (bump_changed() ->
	notify_linuxcnc_reload() -> linuxcnc.command().load_tool_table()
	без аргумента — см. tool_db_daemon.py). qtvcp-виджету вообще не
	нужно знать ни про какой файл или про MDI.
"""
import types
import queue
from qtvcp.core import Status, Info
from qtvcp import qt_tstat
from qtvcp.widgets.tool_offsetview import MyTableModel
from PyQt5.QtCore import Qt, QModelIndex, QVariant, QLocale, QRect, QEvent, QTimer
from PyQt5.QtGui import QColor, QBrush
from PyQt5.QtWidgets import (
	QCheckBox, QItemEditorFactory, QDoubleSpinBox, QSpinBox,
	QStyledItemDelegate, QAbstractSpinBox, QApplication, QStyle,
	QStyleOptionViewItem, QStyleOptionButton, QHeaderView, QAbstractItemView
)
try:
	import linuxcnc
except ImportError:
	linuxcnc = None
	
STATUS = Status()
INFO = Info()
LOG = qt_tstat.LOG

# =====================================================================
# 1. Helpers
# =====================================================================

def _find_column(headerdata, aliases, start=0):
	"""Найти индекс колонки по имени заголовка (регистронезависимо).

	aliases — множество строк-кандидатов (уже lower()).
	start — с какого индекса искать (чтобы не путать переименованные
	стандартные колонки с одноимёнными расширенными).
	Возвращает None, если не найдено — вызывающий код сам решает, что
	делать (свой запасной вариант + предупреждение в лог).
	"""
	for i in range(start, len(headerdata)):
		name = headerdata[i]
		if name is not None and str(name).strip().lower() in aliases:
			return i
	return None

def fmt_trim_zeros(value, decimals):
	"""До `decimals` знаков после точки, без хвостовых нулей, но минимум
	1 знак после точки всегда остаётся (0 -> "0.0", 10 -> "10.0",
	1.5 -> "1.5", не "1.5000000"). Используется и для Z, и для R —
	чтобы не дублировать одну и ту же обрезку в двух местах."""
	text = ('%.*f' % (decimals, float(value))).rstrip('0')
	if text.endswith('.'):
		text += '0'
	return text

# Светло-голубой фон строки текущего инструмента в шпинделе
_SPINDLE_TOOL_BG = QColor(173, 216, 230)  # lightblue

def _brighten_bg(value):
	"""Усиливает подсветку строки текущего в шпинделе инструмента.

	Фреймворк отдаёт для неё бледный/полупрозрачный цвет через
	BackgroundRole — плохо заметный. Не подставляем свой цвет с нуля
	(сломается при смене темы/стиля), а берём ТОТ ЖЕ цвет и поднимаем
	насыщенность и минимальную яркость/непрозрачность. Для остальных
	строк (BackgroundRole не задан/невалиден) возвращаем как есть.

	Сейчас для текущего инструмента используется фиксированный
	светло-голубой (_SPINDLE_TOOL_BG); функция оставлена на случай
	повторного использования для других ролей.
	"""
	color = None
	if isinstance(value, QColor):
		color = value
	elif isinstance(value, QBrush):
		color = value.color()
	if color is None or not color.isValid():
		return value
	h, s, v, a = color.getHsv()
	if h < 0:  # серый/чёрный/белый — без цветового тона, не усиливаем
		return value
	s = min(255, int(s * 2.2))
	v = max(v, 210)
	a = min(255, int(a * 1.3)) if a else 255
	brighter = QColor.fromHsv(h, s, v, a)
	return QBrush(brighter) if isinstance(value, QBrush) else brighter

def operator_message(text, level='ERROR'):
	"""Сообщение оператору в LinuxCNC (error channel + machine log)."""
	msg = str(text)
	if level == 'ERROR':
		LOG.error(msg)
	else:
		LOG.info(msg)
	try:
		if linuxcnc is not None:
			code = getattr(linuxcnc, 'OPERATOR_ERROR', 1) if level == 'ERROR' \
				else getattr(linuxcnc, 'OPERATOR_TEXT', 3)
			STATUS.emit('error', code, msg)
	except Exception:
		pass
	try:
		STATUS.emit('update-machine-log', msg, 'TIME')
	except Exception:
		pass


def seconds_to_hms(sec):
	"""Секунды → 'hh:mm:ss'."""
	try:
		s = int(round(float(sec or 0)))
	except (TypeError, ValueError):
		s = 0
	if s < 0:
		s = 0
	h = s // 3600
	m = (s % 3600) // 60
	sec_r = s % 60
	return '{:02d}:{:02d}:{:02d}'.format(h, m, sec_r)


def hms_to_seconds(value):
	"""'hh:mm:ss' / 'mm:ss' / число секунд → секунды."""
	if value is None:
		return 0.0
	if isinstance(value, (int, float)):
		return float(value)
	s = str(value).strip()
	if not s:
		return 0.0
	if ':' in s:
		parts = s.split(':')
		try:
			parts = [float(p) for p in parts]
		except ValueError:
			raise ValueError('bad runtime format: {!r}'.format(value))
		if len(parts) == 3:
			h, m, sec = parts
		elif len(parts) == 2:
			h, m, sec = 0.0, parts[0], parts[1]
		elif len(parts) == 1:
			h, m, sec = 0.0, 0.0, parts[0]
		else:
			raise ValueError('bad runtime format: {!r}'.format(value))
		return float(h) * 3600.0 + float(m) * 60.0 + float(sec)
	return float(s)
# =====================================================================
# 1b. StatusLabel и инструмент
# ---------------------------------------------------------------------
# Виджеты с tool_comment / tool_diameter / actual_surface_speed и т.п.
# подписаны на STATUS 'tool-info-changed'. Штатный payload — запись
# tool_table (id, diameter, offsets…).
#
#  - tool_comment → TOOL.GET_TOOL_INFO(tno) → наш _reload из БД
#  - tool_diameter / surface_speed → data.diameter (D LinuxCNC = износ)
#
# После любой правки инструмента, который сейчас в шпинделе, шлём
# один tool-info-changed с полями id/diameter (и [0]==id), чтобы
# обновились ВСЕ связанные StatusLabel, а не только comment.
# =====================================================================
def _notify_tool_info_changed(tno, db_client=None, diameter=None, r_diam=None):
	"""Эмит tool-info-changed для StatusLabel + нашего DIAMETER(R).

	Только если tno сейчас в шпинделе. В payload:
	  .id / [0]  — номер инструмента (comment через GET_TOOL_INFO)
	  .diameter  — D износ (штатный tool_diameter_status)
	  .r         — реальный Ø из нашей таблицы (хендлер → lbl DIAMETER)
	"""
	try:
		tno = int(tno)
	except (TypeError, ValueError):
		return
	try:
		cur = int(STATUS.get_current_tool() or 0)
	except Exception:
		cur = -1
	if cur != tno:
		return

	# diameter (D = износ) для tool_diameter_status / surface_speed
	dia = diameter
	r = r_diam
	if db_client is not None and (dia is None or r is None):
		try:
			rec = db_client.get_tool(tno) or {}
			if dia is None:
				dia = float(rec.get('d', rec.get('D', 0.0)) or 0.0)
			if r is None:
				r = float(rec.get('r', rec.get('R', 0.0)) or 0.0)
		except Exception:
			pass
	if dia is None:
		dia = 0.0
	if r is None:
		r = 0.0

	class _ToolRef(object):
		"""Поля status tool_result + .r для нашей подписи DIAMETER."""
		__slots__ = ('id', 'diameter', 'r')
		def __init__(self, n, d, rr):
			self.id = int(n)
			self.diameter = float(d)
			self.r = float(rr)
		def __getitem__(self, i):
			# _tool_file_info: toolnum = tool_entry[0]
			if i == 0:
				return self.id
			raise IndexError(i)

	try:
		STATUS.emit('tool-info-changed', _ToolRef(tno, dia, r))
	except Exception as e:
		LOG.debug('tool-info-changed emit failed: {}'.format(e))


# =====================================================================
# 1. Модель таблицы — настоящее наследование
# =====================================================================
class ToolTableModelExt(MyTableModel):
	EXT_HEADERS = ['Diameter', 'Heavy', 'Big', 'Runtime']  # R H L S
	EXT_FIELD_BY_NAME = {'Diameter': 'R', 'Heavy': 'H', 'Big': 'L', 'Runtime': 'S'}

	def __init__(self, parent, db_client):
		self._db_client = db_client
		self._view = parent
		self._headers_ready = False
		self._random_tc = False
		self._pocket_col = None
		self._z_col = None
		# Безопасные значения ПО УМОЛЧАНИЮ — MyTableModel.__init__() сам
		# вызывает self.update(None) (см. tool_offsetview.py:375) ещё ДО
		# того, как мы ниже успеем посчитать реальные n_std/_tool_col/
		# _comment_col/_letter_by_col по headerdata — а headerdata
		# появляется только ПОСЛЕ super().__init__(). Без этих значений
		# первый же self.update(None) падает с AttributeError на
		# self._tool_col (row[self._tool_col] в цикле prev_order).
		self.n_std = 0
		self._tool_col = 1
		self._comment_col = None
		self._letter_by_col = {}
		super().__init__(parent)
		self.metric_display = bool(INFO.MACHINE_IS_METRIC)

		# После super() headerdata — N стандартных колонок фреймворка.
		# Переименуем "Diameter" (реальный G10 D, используется под нос.
		# радиус/компенсацию) в 'D Wear', чтобы не путать с новой
		# расширенной колонкой "Diameter" (R из внешней БД).
		for i, name in enumerate(self.headerdata):
			if str(name).strip().lower() in ('diameter', 'd', 'diam'):
				self.headerdata[i] = 'D Wear'
				break

		# n_std — фактическое число стандартных колонок фреймворка.
		# Раньше здесь была константа N_STD=20: если версия qtvcp
		# поменяет число стандартных колонок, всё расползётся молча.
		self.n_std = len(self.headerdata)
		self.headerdata = list(self.headerdata) + list(self.EXT_HEADERS)
		self._headers_ready = True

		# Индекс колонки с номером инструмента — ищем по заголовку,
		# а не полагаемся на то, что это всегда row[1].
		self._tool_col = _find_column(
			self.headerdata, {'tool', 'tool number', 't'})
		if self._tool_col is None:
			LOG.error("ToolTableModelExt: колонка 'tool' не найдена по "
					  "заголовку, использую индекс 1 как запасной вариант")
			self._tool_col = 1
			
		# Pocket (P): при RANDOM_TOOL_CHANGER=0 скрываем; при =1 проверяем
		# уникальность. Сам флаг _random_tc обновляется из list_tools().
		self._pocket_col = _find_column(
			self.headerdata, {'pocket', 'p', 'poc'})

		# Индекс колонки Z — нужен, чтобы показывать больше знаков после
		# точки в режиме отображения (не только при редактировании,
		# см. data()/DisplayRole ниже). Точное совпадение 'z', чтобы не
		# зацепить 'z wear'.
		self._z_col = _find_column(self.headerdata, {'z'})

		# Индекс колонки 'D Wear' (см. переименование выше) — нужен, чтобы
		# при правке R брать текущий D локально из arraydata и не делать
		# лишний round-trip get_tool() в _notify_tool_info_changed.
		self._d_col = _find_column(self.headerdata, {'d wear'})
			
		# Индекс колонки Comment — тоже по заголовку, один раз, чтобы
		# не искать её заново на каждый update()/arrange_columns().
		self._comment_col = _find_column(self.headerdata, {'comment'})
		if self._comment_col is None:
			self._comment_col = self.n_std - 1 if self.n_std > 0 else 0

		# Индексы расширенных колонок (Diameter/Heavy/Big/Runtime) —
		# ищем строго в хвосте headerdata (после n_std), чтобы не
		# зацепить переименованный стандартный 'Diameter'->'D Wear'.
		self._letter_by_col = {}
		self._r_col = None
		for name, letter in self.EXT_FIELD_BY_NAME.items():
			col = _find_column(self.headerdata, {name.lower()}, start=self.n_std)
			if col is None:
				LOG.error("ToolTableModelExt: расширенная колонка '{}' "
						  "не найдена в заголовках".format(name))
				continue
			self._letter_by_col[col] = letter
			if letter == 'R':
				self._r_col = col

		# Добить строки arraydata до полной ширины (если update уже отработал)
		self._pad_rows()

	def _known_dia_r(self, row):
		"""Текущие D Wear/R Diameter из уже загруженной строки — чтобы
		вызовы _notify_tool_info_changed после правок, не затрагивающих
		D/R (rename, comment), не делали лишний round-trip get_tool()."""
		dia = r = None
		if self._d_col is not None:
			try:
				dia = float(self.arraydata[row][self._d_col])
			except (TypeError, ValueError, IndexError):
				dia = None
		if self._r_col is not None:
			try:
				r = float(self.arraydata[row][self._r_col])
			except (TypeError, ValueError, IndexError):
				r = None
		return dia, r

	def _ensure_checkboxes(self):
		"""Колонка 0 в wear-модели — всегда QCheckBox.

		MyTableModel.data(CheckStateRole/BackgroundRole) зовёт
		arraydata[row][0].isChecked(). Если там int (стартовый
		arraydata, add_tool, сбой CONVERT) — AttributeError.
		"""
		for row in self.arraydata:
			if not row:
				continue
			if not isinstance(row[0], QCheckBox):
				row[0] = QCheckBox()

	def _pad_rows(self):
		need = self.n_std + len(self.EXT_HEADERS)
		for row in self.arraydata:
			while len(row) < self.n_std:
				row.append(0)
			if len(row) < need:
				row.extend([0.0, False, False, 0.0][:need - len(row)])
			elif len(row) > need:
				del row[need:]
		self._ensure_checkboxes()

	def columnCount(self, parent=None):
		if not getattr(self, '_headers_ready', False):
			return self.n_std
		return self.n_std + len(self.EXT_HEADERS)

	def headerData(self, col, orientation, role):
		if orientation == Qt.Horizontal and role == Qt.DisplayRole:
			if 0 <= col < len(self.headerdata):
				return self.headerdata[col]
			return None
		if orientation != Qt.Horizontal and role == Qt.DisplayRole:
			return ''
		return None

	def update(self, models):
		# Порядок строк до reload — чтобы правка/удаление не
		# пересортировывали таблицу по T ( _reload делает sorted() ).
		prev_order = []
		for row in getattr(self, 'arraydata', None) or []:
			try:
				prev_order.append(int(row[self._tool_col]))
			except (TypeError, ValueError, IndexError):
				pass

		super().update(models)
		if not getattr(self, 'arraydata', None):
			return
			
		if not getattr(self, 'n_std', None):
			return
		
		# Подтянуть расширенные поля из демона
		ext_by_tool = {}
		if self._db_client is not None:
			try:
				# сначала — ответ, который для ЭТОГО ЖЕ цикла обновления
				# уже получил patch_tool_singleton_for_db_program._reload()
				# (см. _shared_list_tools_cache выше). Если его нет
				# (например, update() вызван отдельно от _reload()) —
				# запрашиваем сами, как и раньше.
				resp = _shared_list_tools_cache.pop('resp', None)
				if resp is None:
					resp = self._db_client.list_tools()
				if resp and resp.get('ok'):
					self._random_tc = bool(resp.get('random_tool_changer', False))
					for key, rec in (resp.get('tools') or {}).items():
						try:
							ext_by_tool[int(key)] = rec
						except (TypeError, ValueError):
							continue
			except Exception as e:
				LOG.error('list_tools failed: {}'.format(e))

		need = self.n_std + len(self.EXT_HEADERS)
		for row in self.arraydata:
			while len(row) < self.n_std:
				row.append(0)
			try:
				tno = int(row[self._tool_col])
			except (TypeError, ValueError, IndexError):
				tno = 0
			rec = ext_by_tool.get(tno, {})
			r = float(rec.get('r', rec.get('R', 0.0)) or 0.0)
			h = bool(rec.get('h', rec.get('H', False)))
			l = bool(rec.get('l', rec.get('L', False)))
			# S в БД — секунды; в arraydata тоже секунды, UI — hh:mm:ss
			s_sec = float(rec.get('s', rec.get('S', 0.0)) or 0.0)
			ext = [r, h, l, s_sec]
			if len(row) < need:
				row.extend(ext[:need - len(row)])
			row[self.n_std:need] = ext

		# Восстановить прежний порядок строк по T
		if prev_order:
			by_tno = {}
			for row in self.arraydata:
				try:
					tno = int(row[self._tool_col])
				except (TypeError, ValueError, IndexError):
					continue
				by_tno[tno] = row
			ordered = []
			seen = set()
			for tno in prev_order:
				if tno in by_tno and tno not in seen:
					ordered.append(by_tno[tno])
					seen.add(tno)
			for tno, row in by_tno.items():
				if tno not in seen:
					ordered.append(row)
					seen.add(tno)
			if ordered:
				self.arraydata = ordered

		self._ensure_checkboxes()

		if self._view is not None:
			arrange_columns(self._view, self)

	# Роли оформления, которые есть смысл синхронизировать со стандартной
	# частью строки (подсветка выбранной для удаления строки и т.п.).
	# Qt.CheckStateRole сюда НЕ входит принципиально: если её
	# делегировать, Qt рисует чекбокс в ЛЮБОЙ ячейке, у которой
	# data(..., CheckStateRole) вернула валидное значение — независимо
	# от флагов ItemIsUserCheckable. Так чекбокс колонки 0 ("выбрать для
	# удаления") просочился бы во все расширенные колонки.
	_STYLE_ROLES = (Qt.BackgroundRole, Qt.ForegroundRole)

	def data(self, index, role=Qt.DisplayRole):
		col = index.column()
		if col < self.n_std:
			if col == self._z_col and role == Qt.DisplayRole:
				# Больше знаков после точки в ОБЫЧНОМ (не редактируемом)
				# режиме — сама ячейка/редактор (QDoubleSpinBox,
				# setDecimals(7)) тут не участвует, это чисто текст.
				# Хвостовые нули убираем, но минимум 1 знак после точки
				# оставляем (0 -> "0.0", 1.5 -> "1.5", не "1.5000000").
				raw = super().data(index, Qt.EditRole)
				try:
					return fmt_trim_zeros(raw, 7)
				except (TypeError, ValueError):
					pass
			if role == Qt.TextAlignmentRole and col != self._comment_col:
				# Comment — обычный текст слева, читаемость важнее;
				# всё остальное (числа, tool, pocket) — по центру.
				return Qt.AlignCenter
			if role == Qt.BackgroundRole:
				# строка текущего инструмента — светло-голубой фон
				if self._is_spindle_tool_row(index.row()):
					return _SPINDLE_TOOL_BG
			return super().data(index, role)
			
		try:
			value = self.arraydata[index.row()][col]
		except (IndexError, TypeError):
			return None
			
		letter = self._letter_by_col.get(col)
		if letter in ('H', 'L'):  # Heavy / Big — чекбокс, не текст/комбобокс
			if role == Qt.CheckStateRole:
				return Qt.Checked if value else Qt.Unchecked
			if role == Qt.TextAlignmentRole:
				return Qt.AlignCenter
			if role in self._STYLE_ROLES:
				return self._row_style(index.row(), role)
			# ни DisplayRole, ни EditRole не отдаём текст — иначе Qt
			# по умолчанию создаёт для bool редактор-комбобокс
			# "False"/"True" поверх чекбокса (это и была исходная
			# жалоба)
			return None
			
		if role == Qt.DisplayRole:
			if letter == 'R':  # Diameter
				return fmt_trim_zeros(value or 0, 3)
			if letter == 'S':  # Runtime — hh:mm:ss
				return seconds_to_hms(value)
			return None
			
		if role == Qt.EditRole:
			if letter == 'S':
				return seconds_to_hms(value)
				
			return value
			
		if role == Qt.TextAlignmentRole:
			return Qt.AlignCenter
		if role in self._STYLE_ROLES:
			return self._row_style(index.row(), role)
		return None

	def _is_spindle_tool_row(self, row):
		"""Строка ли это текущего в шпинделе инструмента (а не просто
		отмеченная чекбоксом «выбрать для удаления» — у него тоже своя
		подсветка через BackgroundRole). Только STATUS — без list_tools,
		чтобы не тормозить отрисовку каждой ячейки."""
		try:
			tno = int(self.arraydata[row][self._tool_col])
		except (IndexError, TypeError, ValueError):
			return False
		if tno <= 0:
			return False
		try:
			return int(STATUS.get_current_tool() or 0) == tno
		except Exception:
			return False

	def _row_style(self, row, role):
		"""Оформление строки (подсветка выбранной для удаления строки,
		цвет текста и т.п.) берём со стандартной колонки 0 той же строки —
		иначе расширенные колонки визуально выпадают из подсветки
		(подсвечивалась только стандартная часть строки). Роли,
		относящиеся к чекбоксу/значению/редактированию, сюда не
		передаются — см. _STYLE_ROLES."""
		if role == Qt.BackgroundRole and self._is_spindle_tool_row(row):
			return _SPINDLE_TOOL_BG
		try:
			result = super().data(self.index(row, 0), role)
		except Exception:
			return None
		return result


	def flags(self, index):
		col = index.column()
		if col < self.n_std:
			return super().flags(index)
		base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
		letter = self._letter_by_col.get(col)
		if letter in ('H', 'L'):
			# только чекбокс, без ItemIsEditable — иначе двойной клик
			# всё ещё пытается открыть текстовый/комбобокс-редактор
			return base | Qt.ItemIsUserCheckable
		return base | Qt.ItemIsEditable

	def _find_tool_in_pocket(self, pocket, exclude_tno=None):
		"""Кто занимает карман pocket (исключая exclude_tno). None если свободен."""
		if self._db_client is None:
			return None
			
		try:
			resp = self._db_client.list_tools()
		except Exception as e:
			LOG.error('list_tools failed: {}'.format(e))
			return None
			
		if not resp or not resp.get('ok'):
			return None
			
		# обновить флаг режима с того же ответа
		self._random_tc = bool(resp.get('random_tool_changer', self._random_tc))
		try:
			pocket = int(pocket)
		except (TypeError, ValueError):
			return None
			
		for key, rec in (resp.get('tools') or {}).items():
			try:
				tno = int(key)
			except (TypeError, ValueError):
				continue
				
			if exclude_tno is not None and tno == int(exclude_tno):
				continue
				
			try:
				p = int(rec.get('p', rec.get('P', -1)))
			except (TypeError, ValueError):
				continue
				
			if p == pocket:
				return tno
				
		return None

	def _is_spindle_tool(self, tno):
		"""Инструмент сейчас в шпинделе? STATUS + spindle_tool из демона."""
		try:
			tno = int(tno)
		except (TypeError, ValueError):
			return False
		if tno <= 0:
			return False
		try:
			if int(STATUS.get_current_tool() or 0) == tno:
				return True
		except Exception:
			pass
		if self._db_client is not None:
			try:
				resp = self._db_client.list_tools()
				if resp and resp.get('ok'):
					if int(resp.get('spindle_tool') or 0) == tno:
						return True
			except Exception:
				pass
		return False

	def setData(self, index, value, role=Qt.EditRole):
		if index is None or not index.isValid():
			return False
		if role not in (Qt.EditRole, Qt.CheckStateRole, None):
			return False
		# на случай ручного ввода / другой локали: "1,23" → "1.23"
		if isinstance(value, str) and role in (Qt.EditRole, None):
			value = value.strip().replace(',', '.')			
		col = index.column()
		if col < self.n_std:
			# смена T-номера: UPDATE toolno в БД (не create+delete)
			if (col == self._tool_col and self._db_client is not None
					and role in (Qt.EditRole, None)):
				row = index.row()
				try:
					old_tno = int(self.arraydata[row][self._tool_col])
					new_tno = int(value)
				except (TypeError, ValueError, IndexError):
					return False
				if new_tno < 0:
					return False
				if new_tno != old_tno:
					if self._is_spindle_tool(old_tno):
						operator_message(
							'Cannot change tool number T{}: tool is in the spindle'.format(old_tno))
						return False
					resp = self._db_client.rename_tool(old_tno, new_tno)
					if not resp or not resp.get('ok'):
						err = (resp or {}).get('error') or ''
						if ('already exists' in str(err).lower()):
							operator_message('Tool T{} already exists in tool table'.format(new_tno))
						else:
							operator_message('Failed to rename T{} to T{}: {}'.format(old_tno, new_tno, err or 'unknown error'))
						
						LOG.error('Rename T{} to T{} failed: {}'.format(old_tno, new_tno, (resp or {}).get('error')))
						return False
					self.arraydata[row][self._tool_col] = new_tno
					# RANDOM_TOOL_CHANGER=0: P всегда = T
					if (not self._random_tc
							and self._pocket_col is not None):
						try:
							self.arraydata[row][self._pocket_col] = new_tno
							pidx = self.index(row, self._pocket_col)
							self.dataChanged.emit(pidx, pidx)
						except Exception:
							pass					
					self.dataChanged.emit(index, index)
					dia, r = self._known_dia_r(row)
					_notify_tool_info_changed(new_tno, self._db_client,
											   diameter=dia, r_diam=r)
					return True
				return True

			# смена P при RANDOM_TOOL_CHANGER=1 — уникальность кармана
			if (self._random_tc
					and self._pocket_col is not None
					and col == self._pocket_col
					and self._db_client is not None
					and role in (Qt.EditRole, None)):
				row = index.row()
				try:
					tool_no = int(self.arraydata[row][self._tool_col])
					new_p = int(value)
				except (TypeError, ValueError, IndexError):
					return False
				if new_p < 0:
					operator_message('Wrong pocket P{}'.format(new_p))
					return False
				if self._is_spindle_tool(tool_no):
					operator_message(
						'Cannot change pocket of tool T{}: tool is in the spindle'.format(tool_no))
					return False
				owner = self._find_tool_in_pocket(new_p, exclude_tno=tool_no)
				if owner is not None:
					operator_message('Pocket P{} already has tool T{}'.format(new_p, owner))
					return False
				resp = self._db_client.update_tool(tool_no, {'P': new_p})
				if not resp or not resp.get('ok'):
					err = (resp or {}).get('error') or 'unknown error'
					operator_message('Unable to save change P{} for tool T{}: {}'.format(new_p, tool_no, err))
					return False
				try:
					self.arraydata[row][col] = new_p
				except Exception:
					pass
				ok = super().setData(index, value, role)
				self.dataChanged.emit(index, index)
				return True if ok is None else bool(ok)
					
			# Pocket при любом режиме: нельзя менять у инструмента в шпинделе
			if (self._pocket_col is not None
					and col == self._pocket_col
					and role in (Qt.EditRole, None)
					and not self._random_tc):
				row = index.row()
				try:
					tool_no = int(self.arraydata[row][self._tool_col])
				except (TypeError, ValueError, IndexError):
					return False
				if self._is_spindle_tool(tool_no):
					operator_message(
						'Cannot change pocket of tool T{}: tool is in the spindle'.format(tool_no))
					return False
				# non-random: P=T, ручная смена P запрещена как рассинхрон
				operator_message(
					'Cannot change pocket when RANDOM_TOOL_CHANGER=0 (P must equal T)')
				return False

			# Comment → сразу в БД + tool-info-changed для StatusLabel
			if (self._comment_col is not None
					and col == self._comment_col
					and self._db_client is not None
					and role in (Qt.EditRole, None)):
				row = index.row()
				try:
					tool_no = int(self.arraydata[row][self._tool_col])
				except (IndexError, TypeError, ValueError):
					return False
				comment = '' if value is None else str(value)
				resp = self._db_client.update_tool(tool_no, {'comment': comment})
				if not resp or not resp.get('ok'):
					LOG.error('update_tool comment T{} failed: {}'.format(
						tool_no, (resp or {}).get('error')))
					return False
				try:
					self.arraydata[row][col] = comment
				except Exception:
					pass
				ok = super().setData(index, comment, role)
				self.dataChanged.emit(index, index)
				dia, r = self._known_dia_r(row)
				_notify_tool_info_changed(tool_no, self._db_client,
										   diameter=dia, r_diam=r)
				return True if ok is None else bool(ok)

			return super().setData(index, value, role)
			
		if self._db_client is None:
			return False
		letter = self._letter_by_col.get(col)
		if letter is None:
			return False
		if letter in ('H', 'L') and role != Qt.CheckStateRole:
			return False
		row = index.row()
		try:
			tool_no = int(self.arraydata[row][self._tool_col])
		except (IndexError, TypeError, ValueError):
			return False
		try:
			if letter in ('H', 'L'):
				v = bool(float(value)) if not isinstance(value, bool) else bool(value)
			elif letter == 'S':
				# UI: hh:mm:ss (или число секунд) → БД секунды
				v = hms_to_seconds(value)
			else:
				v = float(value)
		except (TypeError, ValueError) as e:
			if letter == 'S':
				operator_message('Wrong runtime format (use hh:mm:ss): {}'.format(value))
			LOG.error('setData parse error: {}'.format(e))
			return False
		resp = self._db_client.update_tool(tool_no, {letter: v})
		if not resp or not resp.get('ok'):
			LOG.error('update_tool T{} failed: {}'.format(
				tool_no, (resp or {}).get('error')))
			return False
		self.arraydata[row][col] = v
		self.dataChanged.emit(index, index)
		# StatusLabel (.diameter/.r) зависит ТОЛЬКО от R — H/L/S там не
		# участвуют, звать уведомление ради них незачем. Для R передаём
		# уже известные значения (v и текущий D Wear из arraydata),
		# чтобы _notify_tool_info_changed не делала лишний round-trip
		# get_tool() в демон ради того, что мы и так только что записали.
		if letter == 'R':
			dia, _r = self._known_dia_r(row)
			_notify_tool_info_changed(tool_no, self._db_client,
									   diameter=dia, r_diam=v)
		return True

# =====================================================================
# 2. Tool()-синглтон — переопределяем методы на КОНКРЕТНОМ объекте
# =====================================================================

def _std_field_equal(letter, old, new):
	"""Сравнение значения STD-поля для diff'а в _save() (см. ниже) —
	float с допуском на погрешность, comment — строкой."""
	if old is None:
		return False
	if letter == 'comment':
		return str(old) == str(new)
	try:
		return abs(float(old) - float(new)) < 1e-9
	except (TypeError, ValueError):
		return False


# _reload() (STD-поля) и ToolTableModelExt.update() (R/H/L/S) вызываются
# ПОСЛЕДОВАТЕЛЬНО в одном и том же цикле обновления (framework вызывает
# _reload(), затем передаёт результат в update(models)) — оба независимо
# делали свой list_tools() ради ОДНИХ И ТЕХ ЖЕ данных. Однопоточный Qt GUI
# thread, поэтому просто передаём ответ через маленький модульный кэш:
# _reload() кладёт, update() забирает и сразу же удаляет (чтобы никогда не
# читать устаревший ответ на следующем, не связанном с этим, цикле).
_shared_list_tools_cache = {}


def patch_tool_singleton_for_db_program(tool_singleton, db_client):
	# Последнее известное STD-состояние КАЖДОГО инструмента (по данным
	# последнего list_tools() из _reload()) — нужно _save() ниже, чтобы
	# слать update_tool() только для реально изменившихся инструментов,
	# а не для ВСЕХ N при правке одной ячейки (framework отдаёт в _save()
	# снимок ВСЕЙ таблицы на любую правку — см. разбор ниже).
	_last_std_by_tno = {}

	def _reload(self):
		"""Вернуть (tool_model, wear_model) в формате qt_tstat (16 полей):

		[0]=tool [1]=pocket [2]=X [3]=Y [4]=Z [5]=A [6]=B [7]=C
		[8]=U [9]=V [10]=W [11]=D [12]=I [13]=J [14]=Q [15]=comment

		MyTableModel.update() прогонит это через CONVERT_TO_WEAR_TYPE →
		20 колонок с checkbox и wear. Если отдать уже 20 полей,
		CONVERT сдвинет comment и в колонке Comment окажется int.

		Файловая (стандартная) реализация qtvcp читает геометрию, pocket
		и comment из ОДНОЙ строки tool.tbl — они физически не могут
		разойтись, т.к. источник один. Раньше здесь геометрия бралась из
		stat.tool_table, а pocket/comment — из list_tools(): два разных
		источника. Хуже того, stat.tool_table (tool_result) вообще не
		содержит поля pocket — из-за чего в модель попадал фиктивный
		pocket (индекс перечисления), а _save() потом отправлял его
		демону как настоящий P, портя реальное значение в БД при каждом
		сохранении.

		В DB_PROGRAM-конфигурации демон и есть тот самый "единственный
		файл" — LinuxCNC сам строит stat.tool_table из ответов демона по
		протоколу g/p/u, так что делаем ровно то же самое, что файловая
		реализация: одним запросом list_tools() берём ВСЕ поля разом
		(геометрию, pocket, comment) из одной и той же записи — без
		stat.tool_table вообще.
		"""
		tool_model = []
		self.toolinfo = None

		resp = None
		if db_client is not None:
			try:
				resp = db_client.list_tools()
				# отдать тот же ответ ToolTableModelExt.update() ниже по
				# этому же циклу обновления — см. комментарий выше класса.
				_shared_list_tools_cache['resp'] = resp
			except Exception as e:
				LOG.error('list_tools failed: {}'.format(e))

		if not resp or not resp.get('ok'):
			self.toolinfo = [
				0, 0,
				0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
				0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
				0, 'No Tool',
			]
			return (tool_model, [])

		tools = resp.get('tools') or {}

		def fnum(rec, key, cast=float):
			try:
				return cast(rec.get(key, 0) or 0)
			except (TypeError, ValueError):
				return cast(0)

		for key in sorted(tools, key=lambda k: int(k)):
			try:
				tno = int(key)
			except (TypeError, ValueError):
				continue
			if tno < 0:
				continue
			rec = tools[key]
			pocket = fnum(rec, 'p', int)
			comment = str(rec.get('comment', '') or '')
			# РОВНО 16 элементов — как file-based _reload
			row = [
				tno, pocket,
				fnum(rec, 'x'), fnum(rec, 'y'), fnum(rec, 'z'),
				fnum(rec, 'a'), fnum(rec, 'b'), fnum(rec, 'c'),
				fnum(rec, 'u'), fnum(rec, 'v'), fnum(rec, 'w'),
				fnum(rec, 'd'), fnum(rec, 'i'), fnum(rec, 'j'),
				fnum(rec, 'q', int), comment,
			]
			if tno == getattr(self, 'current_tool_num', None):
				self.toolinfo = list(row)
			tool_model.append(row)
			# сохранить как "последнее известное состояние" для _save()
			_last_std_by_tno[tno] = {
				'P': row[1],
				'X': row[2], 'Y': row[3], 'Z': row[4],
				'A': row[5], 'B': row[6], 'C': row[7],
				'U': row[8], 'V': row[9], 'W': row[10],
				'D': row[11], 'I': row[12], 'J': row[13],
				'Q': row[14], 'comment': comment,
			}

		if self.toolinfo is None:
			self.toolinfo = [
				0, 0,
				0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
				0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
				0, 'No Tool',
			]
		return (tool_model, [])

	def _save(self, new_model, delete=()):
		"""new_model уже после CONVERT_TO_STANDARD_TYPE — 16 полей на строку:
		[0]=tool [1]=pocket [2..13]=X Y Z A B C U V W D I J [14]=Q [15]=comment

		Framework вызывает это с ПОЛНЫМ снимком всей таблицы даже при
		правке одной ячейки — поэтому шлём update_tool() только для
		инструментов, чьи STD-поля реально отличаются от последнего
		известного состояния (_last_std_by_tno, см. _reload() выше).
		Без этой проверки редактирование одной ячейки посылало бы
		update_tool() последовательно для КАЖДОГО инструмента в таблице
		(легко подтверждается логами: N упорядоченных upsert подряд на
		одну правку, итоговая задержка ~ N * (время одного upsert)).

		Никакого файла и никакого MDI: в этой конфигурации LinuxCNC
		получает данные инструментов через [EMCIO]DB_PROGRAM =
		tool_db_daemon.py, поэтому изменения просто передаются демону
		тем же db_client, что уже используется для R/H/L/S (см. setData
		модели). Демон сам пишет в свою БД и сам говорит LinuxCNC
		перечитать таблицу (_finalize_write() -> notify_linuxcnc_reload()
		-> linuxcnc.command().load_tool_table() внутри самого демона).
		"""
		if db_client is None:
			self.emit_update()
			return False

		delete_set = set(delete)
		for tno in delete_set:
			_last_std_by_tno.pop(tno, None)
			resp = db_client.delete_tool(tno)
			if not resp or not resp.get('ok'):
				err = (resp or {}).get('error') or 'unknown error'
				LOG.error('delete_tool T{} failed: {}'.format(tno, err))
				# запрет удаления инструмента в шпинделе (демон)
				if 'spindle' in str(err).lower():
					operator_message(
						'Cannot delete tool T{}: tool is in the spindle'.format(tno))
				else:
					operator_message(
						'Cannot delete tool T{}: {}'.format(tno, err))

		# индекс в row -> буква протокола, которую понимает apply_fields
		# демона (Tool.LETTER_MAP) — P/X/Y/Z/A/B/C/U/V/W/D/I/J/Q
		mapping = (
			(1, 'P'),
			(2, 'X'), (3, 'Y'), (4, 'Z'),
			(5, 'A'), (6, 'B'), (7, 'C'),
			(8, 'U'), (9, 'V'), (10, 'W'),
			(11, 'D'), (12, 'I'), (13, 'J'),
			(14, 'Q'),
		)
		for row in new_model:
			try:
				tno = int(row[0])
			except (TypeError, ValueError, IndexError):
				continue
			if tno <= 0 or tno in delete_set:
				continue

			def cell(i, default=0):
				try:
					return row[i]
				except IndexError:
					return default

			fields = {}
			for idx, letter in mapping:
				try:
					fields[letter] = float(cell(idx) or 0)
				except (TypeError, ValueError):
					continue
			# P и Q у демона целые (Tool.LETTER_MAP), но apply_fields сам
			# приводит типы через typ(val) — float(x) для int тоже ок
			fields['comment'] = str(cell(15, '') or '').strip()

			# framework отдаёт сюда снимок ВСЕЙ таблицы при любой правке
			# ОДНОЙ ячейки — без этой проверки редактирование одного
			# инструмента слало бы update_tool() для КАЖДОГО инструмента
			# в таблице (см. тайминги: T1..T30 подряд на одну правку,
			# итоговая задержка ~ N * 24мс). Шлём только реально
			# изменившиеся поля реально изменившихся инструментов.
			prev = _last_std_by_tno.get(tno)
			if prev is not None:
				sent_fields = {
					letter: val for letter, val in fields.items()
					if not _std_field_equal(letter, prev.get(letter), val)
				}
			else:
				# новый/ранее не виденный инструмент — шлём как есть
				sent_fields = fields
			if not sent_fields:
				continue

			resp = db_client.update_tool(tno, sent_fields)
			if not resp or not resp.get('ok'):
				LOG.error('update_tool T{} failed: {}'.format(
					tno, (resp or {}).get('error')))
			else:
				# полным набором fields (не только отправленным diff'ом) —
				# именно таково теперь реальное состояние строки в БД
				_last_std_by_tno.setdefault(tno, {}).update(fields)
				# comment в fields → StatusLabel(tool_comment_status)
				_notify_tool_info_changed(tno, db_client)
			# расширенные R/H/L/S сюда не входят — они пишутся через
			# setData модели сразу при редактировании соответствующей
			# колонки, отдельным вызовом update_tool с другими полями

		self.emit_update()
		return False

	def periodic_check(self, w):
		"""Обновляем таблицу только когда демон реально прислал
		push-уведомление об изменении — никакого blind-опроса.

		ВАЖНО: STATUS.connect('periodic', ...) запоминает bound method
		на момент connect (как clicked→showSelection). Простая подмена
		tool_singleton.periodic_check = ... слот НЕ меняет — остаётся
		штатная проверка md5 tool.tbl, которой в DB_PROGRAM нет.
		Нужны disconnect старого и connect нового (см. ниже).
		"""
		if db_client is None:
			return
		changed = False
		try:
			while True:
				db_client.notify_queue.get_nowait()
				changed = True
		except queue.Empty:
			pass
		if changed and STATUS.is_status_valid():
			self.emit_update()

	tool_singleton._reload = types.MethodType(_reload, tool_singleton)
	tool_singleton._save = types.MethodType(_save, tool_singleton)
	""" STATUS.connect('periodic', ...) запоминает bound method на момент
	 connect и возвращает handler-id (GObject/hal_glib). Подмена
	 tool_singleton.periodic_check = ... слот НЕ меняет — остаётся
	 штатный md5 tool.tbl (в DB_PROGRAM файла нет → emit_update никогда).
	 Отключать старый по id мы не можем (id не сохранён в _TStat),
	 поэтому вешаем ДОПОЛНИТЕЛЬНЫЙ слот на notify_queue. Старый md5-check
	 безвреден (хэш tool.tbl не меняется).	"""
	_new_pc = types.MethodType(periodic_check, tool_singleton)
	tool_singleton.periodic_check = _new_pc
	STATUS.connect('periodic', _new_pc)


# =====================================================================
# 3. showSelection — тоже на конкретном экземпляре виджета
# =====================================================================
def bind_ext_show_selection(view_instance):
	"""Отключаем калькулятор/клавиатуру на клик.

	В ToolOffsetView.createAllView():
	    self.clicked.connect(self.showSelection)
	Сигнал запомнил СТАРЫЙ bound method. Простое
	    view.showSelection = new_method
	слот НЕ меняет — калькулятор продолжает открываться.
	Нужно disconnect старого и connect нового.
	Редактирование: double-click / F2 → in-place editor ячейки.
	"""
	def _showSelection(self, item):
		return

	orig = view_instance.showSelection
	try:
		view_instance.clicked.disconnect(orig)
	except (TypeError, RuntimeError):
		try:
			view_instance.clicked.disconnect()
		except (TypeError, RuntimeError):
			pass

	view_instance.showSelection = types.MethodType(_showSelection, view_instance)
	# не подключаем clicked заново — showSelection больше не нужен


# =====================================================================
# 4. add_tool — тоже на конкретном экземпляре виджета
# =====================================================================
def bind_ext_add_tool(view_instance, db_client):
	"""Переопределяем add_tool на конкретном экземпляре ToolOffsetView.

	Стандартный add_tool() qtvcp дублирует параметры текущей/выбранной
	строки — из-за этого в таблицу попадает "новый" инструмент с теми же
	X/Y/Z/A/B/C..., что и предыдущий (видно в логе: одинаковые X3 Y4 Z5...
	в двух подряд идущих G10 L1). Вместо этого:

	  - находим максимальный существующий номер инструмента (по колонке
		'tool', индекс которой уже определён моделью через заголовок);
	  - создаём новую строку с tool = max+1 и ВСЕМИ остальными полями
		нулевыми/пустыми;
	  - сразу заводим запись в демоне (create_tool), чтобы расширенные
		поля (R/H/L/S) были доступны для правки сразу же;
	  - никакого G10 L1 в этот момент не шлём — пользователь правит поля
		прямо в таблице, а по месту это уже пишет либо setData модели
		(расширенные колонки — сразу в демон), либо обычное сохранение
		таблицы (стандартные колонки — через _save, при выходе из
		редактирования/по кнопке сохранения).
	"""
	def _add_tool(self):
		model = self.model()
		if model is None:
			LOG.error('add_tool: у виджета нет модели')
			return

		tool_col = getattr(model, '_tool_col', 1)
		comment_col = getattr(model, '_comment_col', None)

		max_tool = 0
		for row in model.arraydata:
			try:
				t = int(row[tool_col])
			except (IndexError, TypeError, ValueError):
				continue
			if t > max_tool:
				max_tool = t
		new_tool = max_tool + 1

		ncols = model.columnCount()
		new_row = [0] * ncols
		new_row[0] = QCheckBox()
		new_row[tool_col] = new_tool
		# при добавлении P=T
		pocket_col = getattr(model, '_pocket_col', None)
		if pocket_col is not None and pocket_col < ncols:
			new_row[pocket_col] = new_tool		
		if comment_col is not None and comment_col < ncols:
			new_row[comment_col] = ''

		row_index = len(model.arraydata)
		model.beginInsertRows(QModelIndex(), row_index, row_index)
		model.arraydata.append(new_row)
		model.endInsertRows()

		if db_client is not None:
			try:
				resp = db_client.create_tool(new_tool, {'P': new_tool})
				if not resp or not resp.get('ok'):
					LOG.error('create_tool T{} failed: {}'.format(
						new_tool, (resp or {}).get('error')))
			except Exception as e:
				LOG.error('create_tool T{} exception: {}'.format(new_tool, e))

		try:
			self.selectRow(row_index)
			# прокрутить виджет так, чтобы новая строка (в конце таблицы)
			# была видна целиком: EnsureVisible часто оставляет её
			# обрезанной по нижней кромке viewport — PositionAtBottom
			# ставит строку к низу, а singleShot дожимает scrollbar
			# уже после пересчёта геометрии вставленной строки.
			idx = model.index(row_index, 0)
			if idx.isValid():
				self.scrollTo(idx, QAbstractItemView.PositionAtBottom)
				def _scroll_new_row_fully():
					try:
						self.scrollTo(idx, QAbstractItemView.PositionAtBottom)
						sb = self.verticalScrollBar()
						if sb is not None:
							sb.setValue(sb.maximum())
					except Exception:
						pass
				QTimer.singleShot(0, _scroll_new_row_fully)
		except Exception:
			pass

	view_instance.add_tool = types.MethodType(_add_tool, view_instance)

# =====================================================================
# 4b. редактор чисел: точка как десятичный разделитель
# =====================================================================
# Отображение (Python "%f" / text_template) всегда с точкой.
# Штатный QDoubleSpinBox берёт разделитель из локали системы
# (в ru_RU — запятая) → при правке запятая, в ячейке точка.
# Для tool table / G-code LinuxCNC стандарт — точка (locale "C").
class _CenteredCheckBoxDelegate(QStyledItemDelegate):
	"""Чекбокс по центру ячейки.

	Qt.TextAlignmentRole на положение самого индикатора чекбокса не
	влияет — это ограничение стандартного стиля: CE_ItemViewItem рисует
	check-индикатор всегда у левого края option.rect, TextAlignmentRole
	отвечает только за текст. Поэтому фон/подсветку рисуем как обычно
	через стиль, а сам индикатор — вручную, в прямоугольнике по центру
	ячейки. Клик обрабатываем в editorEvent по тому же прямоугольнику,
	вместо editor'а (тут его нет и не нужно, см. ItemIsUserCheckable
	без ItemIsEditable в модели).
	"""

	def _check_rect(self, option):
		style = option.widget.style() if option.widget else QApplication.style()
		w = style.pixelMetric(QStyle.PM_IndicatorWidth, option, option.widget)
		h = style.pixelMetric(QStyle.PM_IndicatorHeight, option, option.widget)
		x = option.rect.x() + (option.rect.width() - w) // 2
		y = option.rect.y() + (option.rect.height() - h) // 2
		return QRect(x, y, w, h)

	def paint(self, painter, option, index):
		check_state = index.data(Qt.CheckStateRole)
		if check_state is None:
			return super().paint(painter, option, index)

		style = option.widget.style() if option.widget else QApplication.style()

		# фон/подсветка строки — штатно, но без штатного (левого)
		# индикатора и без текста, индикатор дорисуем сами поверх
		opt = QStyleOptionViewItem(option)
		self.initStyleOption(opt, index)
		opt.text = ''
		opt.features &= ~QStyleOptionViewItem.HasCheckIndicator
		style.drawControl(QStyle.CE_ItemViewItem, opt, painter)

		check_opt = QStyleOptionButton()
		check_opt.rect = self._check_rect(option)
		check_opt.state = QStyle.State_Enabled
		check_opt.state |= (QStyle.State_On if check_state == Qt.Checked
							 else QStyle.State_Off)
		style.drawPrimitive(QStyle.PE_IndicatorCheckBox, check_opt, painter)

	def editorEvent(self, event, model, option, index):
		flags = model.flags(index)
		if not (flags & Qt.ItemIsUserCheckable) or not (flags & Qt.ItemIsEnabled):
			return super().editorEvent(event, model, option, index)
		if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
			if self._check_rect(option).contains(event.pos()):
				cur = index.data(Qt.CheckStateRole)
				new_state = Qt.Unchecked if cur == Qt.Checked else Qt.Checked
				model.setData(index, new_state, Qt.CheckStateRole)
			return True
		if event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
			return True
		return super().editorEvent(event, model, option, index)


def bind_centered_checkboxes(view_instance, model):
	"""Ставит _CenteredCheckBoxDelegate на колонки Heavy/Big.

	Персональный делегат на колонку (setItemDelegateForColumn) имеет
	приоритет над делегатом всего view — install_tooltable_number_editors
	это не затрагивает (тот работает с делегатом остальных колонок).
	"""
	if view_instance is None or model is None:
		return
	letter_by_col = getattr(model, '_letter_by_col', {}) or {}
	cols = [col for col, letter in letter_by_col.items() if letter in ('H', 'L')]
	if not cols:
		return
	delegate = _CenteredCheckBoxDelegate(view_instance)
	view_instance._centered_checkbox_delegate = delegate  # держим ссылку живой
	for col in cols:
		view_instance.setItemDelegateForColumn(col, delegate)


class _TrimmedDoubleSpinBox(QDoubleSpinBox):
	"""QDoubleSpinBox, который показывает значение без хвостовых нулей
	после точки (до setDecimals() знаков), но минимум один знак после
	точки — целое число выглядит как "5.0", а не "5" и не
	"5.0000000". Та же логика, что и fmt_trim_zeros() для отображения
	ячейки вне редактирования — просто здесь textFromValue/
	valueFromText, а не data()."""

	def textFromValue(self, value):
		return fmt_trim_zeros(value, self.decimals())

	def valueFromText(self, text):
		# и ',' и '.' — десятичный разделитель (locale C / G-code)
		text = (text or '').strip().replace(',', '.')
		if not text or text in ('-', '.', '-.'):
			return 0.0
		try:
			return float(text)
		except ValueError:
			return 0.0

	def validate(self, text, pos):
		# C-locale отвергает ',': проверяем с '.', в поле оставляем ввод оператора
		fixed = (text or '').replace(',', '.')
		state, _, newpos = super().validate(fixed, pos)
		return state, text, newpos

	def fixup(self, text):
		return super().fix((text or '').replace(',', '.'))

	def keyPressEvent(self, event):
		# запятая на клавиатуре → точка
		if event.text() == ',':
			from PyQt5.QtGui import QKeyEvent
			event = QKeyEvent(
				event.type(), Qt.Key_Period, event.modifiers(),
				'.', event.isAutoRepeat(), event.count())
		super().keyPressEvent(event)


class _ToolTableItemEditorFactory(QItemEditorFactory):
	def createEditor(self, userType, parent):
		if userType == QVariant.Double:
			box = _TrimmedDoubleSpinBox(parent)
			box.setLocale(QLocale.c())  # decimal point = '.'
			box.setDecimals(7)
			box.setMaximum(99999)
			box.setMinimum(-99999)
			box.setButtonSymbols(QAbstractSpinBox.NoButtons)
			return box
		if userType == QVariant.Int:
			box = QSpinBox(parent)
			box.setLocale(QLocale.c())
			box.setMaximum(20000)
			box.setMinimum(0)
			box.setButtonSymbols(QAbstractSpinBox.NoButtons)
			return box
		return super(_ToolTableItemEditorFactory, self).createEditor(
			userType, parent)


def install_tooltable_number_editors(view):
	"""Только setItemEditorFactory на УЖЕ существующем delegate.

	Нельзя каждый раз делать setItemDelegate(новый): ломается
	закрытие редактора по Enter (delegate/eventFilter сбрасываются).
	Вызывать один раз на виджет.
	"""
	if view is None:
		return
	if getattr(view, '_tooltable_c_locale_editors', False):
		return
	try:
		delegate = view.itemDelegate()
		if delegate is None:
			delegate = QStyledItemDelegate(view)
			view.setItemDelegate(delegate)
		if hasattr(delegate, 'setItemEditorFactory'):
			delegate.setItemEditorFactory(_ToolTableItemEditorFactory())
		view._tooltable_c_locale_editors = True
	except Exception as e:
		LOG.error('install_tooltable_number_editors: {}'.format(e))


def arrange_columns(view, model=None):
	# колонка Comment отображается последней
	# при RANDOM_TOOL_CHANGER=0 скрываем колонку Pocket (P)
	# мин. ширина X/Y/Z — 6 символов
	try:
		header = view.horizontalHeader()
		if model is None:
			model = view.model()
		if model is None:
			return

		# точка как decimal separator в редакторе (не запятая локали)
		install_tooltable_number_editors(view)

		# Pocket
		pocket_col = getattr(model, '_pocket_col', None)
		random_tc = getattr(model, '_random_tc', False)
		if pocket_col is not None:
			if random_tc:
				view.showColumn(pocket_col)
			else:
				view.hideColumn(pocket_col)

		# Comment
		comment_logical = getattr(model, '_comment_col', None)
		if comment_logical is None:
			ncols = model.columnCount()
			comment_logical = 19 if ncols > 19 else ncols - 1

		last_visual = header.count() - 1
		if last_visual < 0:
			return
		cur = header.visualIndex(comment_logical)
		if cur >= 0 and cur != last_visual:
			header.moveSection(cur, last_visual)

		# мин. ширина X/Y/Z — 6 символов, Z — на 3 символа шире остальных.
		# В createAllView() стоит hh.setSectionResizeMode(3) =
		# ResizeToContents → setColumnWidth игнорируется. Для этих
		# колонок переключаем режим на Interactive и задаём ширину.
		fm = view.fontMetrics()
		char_w = (fm.horizontalAdvance('0')
				  if hasattr(fm, 'horizontalAdvance') else fm.width('0'))
		# 6 значащих + запас под знак/точку/padding шаблона %10.3f
		min_w = max(int(char_w * 6) + 8, 1)
		min_w_z = max(int(char_w * 12) + 8, 1)  # Z шире — под 7 знаков после точки
		hdr = list(getattr(model, 'headerdata', []) or [])
		for axis in ('x', 'y', 'z'):
			col = None
			for i, name in enumerate(hdr):
				n = str(name or '').strip().lower()
				# 'x', 'x r', 'x d' — основная ось; не 'x wear'
				if n == axis or n.startswith(axis + ' ') and 'wear' not in n:
					col = i
					break
			if col is None or view.isColumnHidden(col):
				continue
			try:
				header.setSectionResizeMode(col, QHeaderView.Interactive)
			except Exception:
				pass
			target_w = min_w_z if axis == 'z' else min_w
			cur_w = view.columnWidth(col)
			if cur_w < target_w:
				view.setColumnWidth(col, target_w)
	except Exception:
		pass
