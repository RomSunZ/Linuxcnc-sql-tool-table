#!/usr/bin/env python3
"""
tool_db_daemon.py — база данных инструментов LinuxCNC на SQLite.
Полностью заменяет tool.tbl через [EMCIO]DB_PROGRAM (interface v2.1),
используя штатный модуль tooldb (lib/python/tooldb.py).

Работает одинаково и с ручной сменой (RANDOM_TOOL_CHANGER=0, manual_change.ngc),
и с random ATC (RANDOM_TOOL_CHANGER=1) — режим определяется автоматически
по INI активной конфигурации.

Если файл базы данных не найден при старте — создаётся новый, со схемой и
единственным инструментом T0/P0/"Empty".

Все поля инструмента именуются буквами протокола — так же, как стандартные
поля LinuxCNC (T,P,X,Y,Z,A,B,C,U,V,W,D,I,J,Q):
  P -> pocket (карман, 0 = в шпинделе)
  R -> реальный диаметр инструмента
  H -> тяжёлый инструмент
  L -> крупный инструмент, занимает соседние карманы
  S -> наработка резанием, сек
Единственное текстовое поле — comment (;comment) — оно и в стандартном
tool.tbl не буква, а отдельный маркер до конца строки.
R/H/L/S — расширение, которого LinuxCNC не видит вовсе (как и 'M' в
официальном db_demo/db.py) — они никогда не попадают в ответ на 'g'.

Инструмент, находящийся в шпинделе, хранится в таблице state (переживает
перезапуск демона). Если в шпинделе ничего нет — это T0.

Путь к базе данных — argv[1], тот же самый параметр из INI:
  [EMCIO]
  DB_PROGRAM = ./tool_db_daemon.py /abs/path/tool_db.sqlite3
  RANDOM_TOOL_CHANGER = 0   ; или 1 для random ATC

Виджет qtDragon общается с демоном через unix-сокет <db_path>.sock,
построчный JSON, держит ОДНО постоянное соединение:
  запрос:  {"op":"update","toolno":5,"fields":{"R":6.0,"H":1,"comment":"..."}}
  запрос:  {"op":"create","toolno":7,"fields":{"comment":"new drill"}}
  запрос:  {"op":"delete","toolno":7}
  запрос:  {"op":"list"}
  ответ:   {"ok":true,...} / {"ok":false,"error":"..."}
  push (без запроса, всем подключённым клиентам при любом изменении БД):
		   {"event":"changed","seq":42}

сделайте скрипт исполняемым (chmod +x tool_db_daemon.py)
"""
import sys, os, json, socket, threading, sqlite3
from dataclasses import dataclass

from tooldb import tooldb_callbacks   # функции (g,p,l,u)
from tooldb import tooldb_tools       # список номеров инструментов
from tooldb import tooldb_loop        # главный цикл

try:
	import hal
except ImportError:
	hal = None

try:
	import linuxcnc
except ImportError:
	linuxcnc = None

"""
motion.motion-type OUT S32
0: Idle (no motion)  
1: Traverse  
2: Linear feed  
3: Arc feed
4: Tool change  
5: Probing  
6: Rotary unlock for traverse
"""
MOTION_LINEAR_FEED = 2
MOTION_ARC_FEED = 3
CUTTING_MOTION_TYPES = (MOTION_LINEAR_FEED, MOTION_ARC_FEED)

if len(sys.argv) < 2:
	sys.stderr.write("usage: tool_db_daemon.py <tool_db.sql>\n")
	sys.exit(1)
DB_PATH = os.path.abspath(sys.argv[1])
SOCK_PATH = DB_PATH + ".sock"


def msg(txt):
	sys.stderr.write("tool_db_daemon: %s\n" % txt)
	sys.stderr.flush()


def detect_random_toolchanger():
	ini_path = os.environ.get('INI_FILE_NAME')
	if not ini_path or not os.path.exists(ini_path) or linuxcnc is None:
		msg("RANDOM_TOOL_CHANGER not determinable → 0 (manual)")
		return False
	try:
		ini = linuxcnc.ini(ini_path)
		raw = ini.find('EMCIO', 'RANDOM_TOOL_CHANGER') or '0'
		# linuxcnc.ini часто отдаёт "0 ; комментарий" целиком —
		# отрезаем ;comment и берём первое слово
		val = str(raw).split(';', 1)[0].strip().split()[0] if str(raw).strip() else '0'
		random_tc = val.upper() not in ('0', '', 'FALSE', 'NO', 'OFF')
		msg("RANDOM_TOOL_CHANGER=%r (raw=%r) → random_tc=%s" % (val, raw, random_tc))
		return random_tc
	except Exception as e:
		msg("detect_random_toolchanger failed: %s → 0" % e)
		return False


RANDOM_TC = detect_random_toolchanger()


# =====================================================================
# Tool — вся информация об одном инструменте, только буквенные поля
# =====================================================================
@dataclass
class Tool:
	tno: int
	p: int = 0			# pocket
	x: float = 0.0		# офсеты
	y: float = 0.0
	z: float = 0.0
	a: float = 0.0
	b: float = 0.0
	c: float = 0.0
	u: float = 0.0
	v: float = 0.0
	w: float = 0.0
	d: float = 0.0		# износ инструмента
	i: float = 0.0		# передний угол токарного резца			
	j: float = 0.0		# задний угол токарного резца	
	q: int = 0			# положение токарного резца (0-8)
	comment: str = ""  
	r: float = 0.0		# диаметр инструмента
	h: bool = False		# тяжёлый инструмент
	l: bool = False		# крупный инструмент, освободить соседние ячейки
	s: float = 0.0		# S — наработка инструмента, сек

	STD_COLUMNS = ('p', 'x', 'y', 'z', 'a', 'b', 'c', 'u', 'v', 'w',
				   'd', 'i', 'j', 'q', 'comment')
	EXT_COLUMNS = ('r', 'h', 'l', 's')
	ALL_COLUMNS = STD_COLUMNS + EXT_COLUMNS   # без toolno — он PRIMARY KEY

	# буква протокола -> (имя атрибута, тип). 'T' и ';comment' сюда
	# намеренно не входят — они обрабатываются отдельно (см. from_params).
	LETTER_MAP = {
		'P': ('p', int),
		'X': ('x', float), 'Y': ('y', float), 'Z': ('z', float),
		'A': ('a', float), 'B': ('b', float), 'C': ('c', float),
		'U': ('u', float), 'V': ('v', float), 'W': ('w', float),
		'D': ('d', float), 'I': ('i', float), 'J': ('j', float), 'Q': ('q', int),
		'R': ('r', float), 'H': ('h', bool), 'L': ('l', bool), 'S': ('s', float),
	}

	@classmethod
	def empty(cls, tno, pocket=None, comment=""):
		return cls(tno=tno, p=pocket if pocket is not None else tno,
					comment=comment)

	@classmethod
	def from_row(cls, row):
		"""row — sqlite3.Row (доступ по имени колонки)."""
		kwargs = {'tno': row['toolno']}
		for col in cls.ALL_COLUMNS:
			val = row[col]
			kwargs[col] = bool(val) if col in ('h', 'l') else val
		return cls(**kwargs)

	def to_row_dict(self):
		d = {}
		for col in self.ALL_COLUMNS:
			val = getattr(self, col)
			d[col] = (1 if val else 0) if col in ('h', 'l') else val
		return d

	@classmethod
	def from_params(cls, tno, params, base=None):
		"""Разобрать строку вида 'T1 P1 X0 D0.125 R6.0 H1 ;comment' в Tool.
		base — существующий Tool для частичного обновления (put меняет не
		все поля разом, как и G10 L1/L10/L11 в реальном G-коде)."""
		obj = base if base is not None else cls(tno=tno, p=tno)
		obj.tno = tno
		body, _, comment = params.partition(';')
		for tok in body.upper().split():
			if not tok or tok[0] == 'T':
				continue
			letter, raw = tok[0], tok[1:]
			spec = cls.LETTER_MAP.get(letter)
			if not spec or not raw:
				continue
			attr, typ = spec
			try:
				setattr(obj, attr, bool(float(raw)) if typ is bool else typ(raw))
			except ValueError:
				continue
		if comment.strip():
			obj.comment = comment.strip()
		return obj

	def apply_fields(self, fields):
		"""Правка от виджета/сокета — ключи ТОЛЬКО буквы протокола (в любом
		регистре), плюс 'comment' как единственное текстовое исключение —
		так же, как в стандартном tool.tbl."""
		for key, val in fields.items():
			k = str(key).strip()
			if k.lower() == 'comment':
				self.comment = str(val)
				continue
			spec = self.LETTER_MAP.get(k.upper())
			if spec:
				attr, typ = spec
				setattr(self, attr, bool(val) if typ is bool else typ(val))

	def to_lcnc_line(self):
		"""Строка, которую понимает LinuxCNC (ответ на 'g') — ТОЛЬКО
		стандартные буквы. R/H/L/S сюда никогда не попадают — LinuxCNC
		про них знать не должен."""
		parts = ["T%d" % self.tno, "P%d" % int(self.p)]
		for letter in ('X', 'Y', 'Z', 'A', 'B', 'C', 'U', 'V', 'W', 'I', 'J'):
			attr, _ = self.LETTER_MAP[letter]
			val = getattr(self, attr)
			if val:
				parts.append("%s%g" % (letter, val))
		parts.append("D%g" % self.d)   # всегда, даже 0 — это износ, не диаметр
		if self.q:
			parts.append("Q%d" % int(self.q))
		line = " ".join(parts)
		if self.comment:
			line += " ;%s" % self.comment
		return line


# =====================================================================
# HAL — пины для G-кода (#<_hal[toolext.r]> и т.п.), НЕ для уведомлений
# =====================================================================
halcomp = None
if hal is not None:
	try:
		halcomp = hal.component("toolext")
		halcomp.newpin("r", hal.HAL_FLOAT, hal.HAL_OUT)   # диаметр
		halcomp.newpin("h", hal.HAL_BIT, hal.HAL_OUT)     # тяжёлый
		halcomp.newpin("l", hal.HAL_BIT, hal.HAL_OUT)     # крупный
		halcomp.newpin("s", hal.HAL_FLOAT, hal.HAL_OUT)   # наработка
		halcomp.ready()
	except Exception as e:
		msg("HAL component failed: %s" % e)
		halcomp = None


def update_hal_pins(tool):
	if halcomp is None:
		return
	if tool is None:
		tool = Tool.empty(0)
	halcomp['r'] = float(tool.r)
	halcomp['h'] = bool(tool.h)
	halcomp['l'] = bool(tool.l)
	halcomp['s'] = float(tool.s)


# =====================================================================
# сокет: запросы от виджета + push-уведомления об изменениях (вариант 2)
# =====================================================================
_subscribers = []
_subscribers_lock = threading.Lock()


def notify_subscribers(seq):
	"""Push без блокировки tooldb_loop. Мёртвые клиенты отбрасываются."""
	line = (json.dumps({"event": "changed", "seq": seq}) + "\n").encode("utf-8")
	with _subscribers_lock:
		dead = []
		for conn in _subscribers:
			try:
				conn.setblocking(False)
				conn.sendall(line)
			except (BlockingIOError, InterruptedError):
				# буфер полон — клиент не читает, считаем мёртвым
				dead.append(conn)
			except (ConnectionResetError, BrokenPipeError, ConnectionError, OSError):
				dead.append(conn)
			finally:
				try:
					conn.setblocking(True)
				except OSError:
					pass
		for conn in dead:
			try:
				_subscribers.remove(conn)
			except ValueError:
				pass
			try:
				conn.close()
			except OSError:
				pass


def process_request(req):
	op = req.get("op")
	tno = req.get("toolno")

	if op == "list":
		tools = {n: db.get(n).to_row_dict() for n in db.all_tool_numbers()}
		return {"ok": True, "tools": tools, "spindle_tool": db.get_spindle_tool(),
				"random_tool_changer": bool(RANDOM_TC)}

	if op == "create":
		if tno is None:
			return {"ok": False, "error": "toolno required"}
		tno = int(tno)
		if db.get(tno) is not None:
			return {"ok": False, "error": "tool %d already exists" % tno}
		t = Tool.empty(tno)
		t.apply_fields(req.get("fields") or {})
		t.p = tno
		db.upsert(t)
		return {"ok": True, "tool": t.to_row_dict()}

	if op == "update":
		if tno is None:
			return {"ok": False, "error": "toolno required"}
		t = db.apply_fields(int(tno), req.get("fields") or {})
		return {"ok": True, "tool": t.to_row_dict()}

	if op == "delete":
		if tno is None:
			return {"ok": False, "error": "toolno required"}
		try:
			db.delete(int(tno))
		except ValueError as e:
			return {"ok": False, "error": str(e)}
		return {"ok": True}

	if op == "rename":
		# {"op":"rename","toolno":1,"new_toolno":5}
		new_tno = req.get("new_toolno")
		if tno is None or new_tno is None:
			return {"ok": False, "error": "toolno and new_toolno required"}
		try:
			t = db.rename(int(tno), int(new_tno))
		except ValueError as e:
			return {"ok": False, "error": str(e)}
		return {"ok": True, "tool": t.to_row_dict() if t else {}}

	return {"ok": False, "error": "unknown op: %s" % op}


def handle_client(conn):
	"""Обслуживание одного виджета. Обрыв сокета (reset/peer close) — норма."""
	with _subscribers_lock:
		_subscribers.append(conn)
	try:
		buf = b""
		while True:
			try:
				chunk = conn.recv(4096)
			except (ConnectionResetError, BrokenPipeError, ConnectionError, OSError):
				break
			if not chunk:
				break
			buf += chunk
			while b"\n" in buf:
				line, buf = buf.split(b"\n", 1)
				if not line.strip():
					continue
				try:
					req = json.loads(line.decode("utf-8"))
					resp = process_request(req)
				except Exception as e:
					resp = {"ok": False, "error": str(e)}
				try:
					conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
				except (ConnectionResetError, BrokenPipeError, ConnectionError, OSError):
					break
	except (ConnectionResetError, BrokenPipeError, ConnectionError, OSError):
		pass
	finally:
		with _subscribers_lock:
			if conn in _subscribers:
				_subscribers.remove(conn)
		try:
			conn.close()
		except OSError:
			pass


def start_socket_server(sock_path):
	try:
		if os.path.exists(sock_path):
			os.remove(sock_path)
	except OSError:
		pass
	srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
	srv.bind(sock_path)
	srv.listen(5)

	def accept_loop():
		while True:
			try:
				conn, _ = srv.accept()
			except OSError:
				break
			try:
				conn.settimeout(30.0)
			except OSError:
				pass
			threading.Thread(target=handle_client, args=(conn,),
							  daemon=True).start()

	threading.Thread(target=accept_loop, daemon=True).start()
	msg("widget socket listening at %s" % sock_path)
	return srv


# =====================================================================
# ToolDB — sqlite-бэкенд, реестр номеров, состояние шпинделя
# =====================================================================
class ToolDB:
	def __init__(self, path):
		self.path = path
		self._lock = threading.Lock()
		self._lcnc_cmd = None
		self._known_set = None
		self._watch_timer = None

		need_init = not os.path.exists(path)
		self.conn = sqlite3.connect(path, check_same_thread=False)
		self.conn.row_factory = sqlite3.Row
		self.conn.execute("PRAGMA journal_mode=WAL")
		self._create_schema()
		if need_init:
			msg("database not found → creating new with default T0/P0/Empty")
			self.upsert(Tool.empty(0))

		self._reconcile_registration()
		self._external_change_guard = self._current_mtime()
		self._start_watch()

	def _current_mtime(self):
		best = 0.0
		for p in (self.path, self.path + "-wal"):
			try:
				best = max(best, os.stat(p).st_mtime)
			except OSError:
				pass
		return best

	def _create_schema(self):
		def sqltype(col):
			if col in ('p', 'q'):
				return 'INTEGER'
			if col == 'comment':
				return 'TEXT'
			if col in ('h', 'l'):
				return 'INTEGER'
			return 'REAL'
		cols_sql = ", ".join("%s %s" % (col, sqltype(col))
							  for col in Tool.ALL_COLUMNS)
		with self._lock:
			self.conn.execute(f"CREATE TABLE IF NOT EXISTS tools "
							   f"(toolno INTEGER PRIMARY KEY, {cols_sql})")
			self.conn.execute("CREATE TABLE IF NOT EXISTS state "
							   "(key TEXT PRIMARY KEY, value TEXT)")
			self.conn.execute("INSERT OR IGNORE INTO state(key,value) "
							   "VALUES ('spindle_tool','0')")
			self.conn.execute("INSERT OR IGNORE INTO state(key,value) "
							   "VALUES ('changed_seq','0')")
			self.conn.commit()

	# ------------------------------------------------------------ CRUD
	def get(self, tno):
		with self._lock:
			row = self.conn.execute(
				"SELECT * FROM tools WHERE toolno=?", (tno,)).fetchone()
		return Tool.from_row(row) if row else None

	def get_line(self, tno):
		t = self.get(tno) or Tool.empty(tno)
		return t.to_lcnc_line()

	def all_tool_numbers(self):
		with self._lock:
			rows = self.conn.execute("SELECT toolno FROM tools").fetchall()
		return sorted(r['toolno'] for r in rows)

	def upsert(self, tool, from_linuxcnc=False):
		d = tool.to_row_dict()
		cols = list(d.keys())
		collist = ", ".join(cols)
		placeholders = ", ".join("?" for _ in cols)
		updates = ", ".join("%s=excluded.%s" % (c, c) for c in cols)
		sql = (f"INSERT INTO tools(toolno,{collist}) VALUES(?,{placeholders}) "
			   f"ON CONFLICT(toolno) DO UPDATE SET {updates}")
		with self._lock:
			self.conn.execute(sql, [tool.tno] + [d[c] for c in cols])
			self.conn.commit()
			self._external_change_guard = self._current_mtime()
		self.bump_changed(from_linuxcnc=from_linuxcnc)

	def apply_from_wire_params(self, tno, params):
		"""G10 L1/L10/L11 — буквенный протокол.
		from_linuxcnc=True: не вызывать load_tool_table — иначе deadlock
		(LinuxCNC ждёт FINI put, а мы просим его снова читать таблицу).
		"""
		t = Tool.from_params(tno, params, base=self.get(tno))
		self.upsert(t, from_linuxcnc=True)
		return t

	def apply_fields(self, tno, fields):
		"""Правка из сокета — тоже только буквы (см. Tool.apply_fields)."""
		t = self.get(tno) or Tool(tno=tno, p=tno)
		t.apply_fields(fields)
		self.upsert(t)
		return t

	def delete(self, tno):
		if tno == 0:
			raise ValueError("Cannot delete empty tool T0")
		if tno == self.get_spindle_tool():
			raise ValueError("Tool T%d is now in spindle" % tno)
		with self._lock:
			self.conn.execute("DELETE FROM tools WHERE toolno=?", (tno,))
			self.conn.commit()
			self._external_change_guard = self._current_mtime()
		self.bump_changed()

	def rename(self, old_tno, new_tno):
		"""Сменить PRIMARY KEY toolno in-place (без create/delete)."""
		old_tno, new_tno = int(old_tno), int(new_tno)
		if old_tno == new_tno:
			return self.get(old_tno)
		if new_tno < 0:
			raise ValueError("Wrong tool number %d" % new_tno)
		if old_tno == 0:
			raise ValueError("Cannot rename tool T0")
		if new_tno == 0:
			raise ValueError("Cannot rename tool T%d to T0" % old_tno)
		if self.get(old_tno) is None:
			raise ValueError("Cannot find tool T%d" % old_tno)
		if self.get(new_tno) is not None:
			raise ValueError("Tool T%d already exists" % new_tno)
		if old_tno == self.get_spindle_tool():
			# обновим spindle_tool вместе с номером
			pass
		with self._lock:
			self.conn.execute(
				"UPDATE tools SET toolno=? WHERE toolno=?",
				(new_tno, old_tno))
			if old_tno == int(self.conn.execute(
					"SELECT value FROM state WHERE key='spindle_tool'"
					).fetchone()['value']):
				self.conn.execute(
					"UPDATE state SET value=? WHERE key='spindle_tool'",
					(str(new_tno),))
			self.conn.commit()
			self._external_change_guard = self._current_mtime()
		self.bump_changed()
		return self.get(new_tno)


	def set_pocket(self, tno, pocket):
		with self._lock:
			self.conn.execute(
				"UPDATE tools SET p=? WHERE toolno=?", (pocket, tno))
			self.conn.commit()
			self._external_change_guard = self._current_mtime()

	def add_runtime(self, tno, delta):
		"""Наработка S. LinuxCNC это поле не читает (нет в ответе 'g'),
		поэтому load_tool_table() не нужен — достаточно push виджету,
		иначе колонка Runtime в таблице не обновится."""
		with self._lock:
			self.conn.execute(
				"UPDATE tools SET s = s + ? WHERE toolno=?", (delta, tno))
			self.conn.execute(
				"UPDATE state SET value = CAST(value AS INTEGER) + 1 "
				"WHERE key='changed_seq'")
			self.conn.commit()
			self._external_change_guard = self._current_mtime()
			row = self.conn.execute(
				"SELECT value FROM state WHERE key='changed_seq'").fetchone()
			seq = int(row['value'])
		notify_subscribers(seq)
	# ------------------------------------------------------------ шпиндель
	def get_spindle_tool(self):
		with self._lock:
			row = self.conn.execute(
				"SELECT value FROM state WHERE key='spindle_tool'").fetchone()
		return int(row['value']) if row else 0

	def set_spindle_tool(self, tno):
		with self._lock:
			self.conn.execute(
				"UPDATE state SET value=? WHERE key='spindle_tool'",
				(str(tno),))
			self.conn.commit()
			self._external_change_guard = self._current_mtime()

	# ------------------------------------------------------------ реестр для tooldb_tools()
	def _reconcile_registration(self):
		current = self.all_tool_numbers()
		if self._known_set is not None and current != self._known_set:
			tooldb_tools(current)
			msg("tool set changed → re-registered: %s" % current)
		self._known_set = current

	# ------------------------------------------------------------ уведомления
	def bump_changed(self, from_linuxcnc=False):
		with self._lock:
			self.conn.execute(
				"UPDATE state SET value = CAST(value AS INTEGER) + 1 "
				"WHERE key='changed_seq'")
			self.conn.commit()
			# ВАЖНО: после СВОЕГО commit обновляем guard, иначе 1с-watcher
			# увидит смену mtime/-wal и вызовет load_tool_table() —
			# в AUTO это даёт «не могу делать это (EMC_TOOL_LOAD_TOOL_TABLE)».
			self._external_change_guard = self._current_mtime()
			row = self.conn.execute(
				"SELECT value FROM state WHERE key='changed_seq'").fetchone()
			seq = int(row['value'])
		self._reconcile_registration()
		# load_tool_table ТОЛЬКО если изменение пришло НЕ из put LinuxCNC
		# (put уже передал данные интерпретатору; повторный reload в AUTO
		# запрещён при читающем интерпретаторе).
		if not from_linuxcnc:
			self.notify_linuxcnc_reload()
		notify_subscribers(seq)

	def notify_linuxcnc_reload(self):
		"""Перечитать таблицу инструментов в LinuxCNC.

		В AUTO с читающим интерпретатором EMC_TOOL_LOAD_TOOL_TABLE
		запрещён (ошибка «не могу делать это … в авто режиме»).
		Пропускаем вызов; данные из put/G10 уже в интерпретаторе,
		а виджет обновится через push notify_subscribers.
		"""
		if linuxcnc is None:
			return
		try:
			s = linuxcnc.stat()
			s.poll()
			# MODE_AUTO == 2; INTERP_READING / INTERP_WAITING — активный УП
			if s.task_mode == linuxcnc.MODE_AUTO and s.interp_state != linuxcnc.INTERP_IDLE:
				msg("notify_linuxcnc_reload skipped: AUTO + interp busy "
					"(task_mode=%s interp_state=%s)" % (s.task_mode, s.interp_state))
				return
		except Exception as e:
			msg("notify_linuxcnc_reload stat poll failed: %s" % e)
			# при сбое poll безопаснее не слать reload в неизвестном состоянии
			return
		try:
			if self._lcnc_cmd is None:
				self._lcnc_cmd = linuxcnc.command()
			self._lcnc_cmd.load_tool_table()
		except Exception as e:
			msg("notify_linuxcnc_reload failed: %s" % e)

	# ------------------------------------------------------------ внешние изменения файла
	def _start_watch(self):
		self._check_external_change()

	def _check_external_change(self):
		mtime = self._current_mtime()
		if mtime != self._external_change_guard:
			msg("external change to %s detected → reloading" % self.path)
			self._external_change_guard = mtime
			self._reconcile_registration()
			with self._lock:
				row = self.conn.execute(
					"SELECT value FROM state WHERE key='changed_seq'").fetchone()
			seq = int(row['value']) if row else 0
			notify_subscribers(seq)
			self.notify_linuxcnc_reload()
		self._watch_timer = threading.Timer(1.0, self._check_external_change)
		self._watch_timer.daemon = True
		self._watch_timer.start()


db = ToolDB(DB_PATH)
socket_server = start_socket_server(SOCK_PATH)


# =====================================================================
# tooldb callbacks
# =====================================================================
def user_get_tool(tno):
	return db.get_line(tno)


def user_put_tool(tno, params):
	"""G10 L1 / L10 / L11"""
	t = db.apply_from_wire_params(tno, params)
	if tno == db.get_spindle_tool():
		update_hal_pins(t)
	msg("put T%d → %s" % (tno, params))


def _parse_tp(params):
	t = p = None
	for tok in params.upper().split():
		if tok.startswith('T'):
			t = int(tok[1:])
		elif tok.startswith('P'):
			p = int(tok[1:])
	return t, p


def user_load_spindle_nonran(tno, params):
	db.set_spindle_tool(tno)
	update_hal_pins(db.get(tno))


def user_unload_spindle_nonran(tno, params):
	db.set_spindle_tool(0)
	update_hal_pins(db.get(0))


def user_load_spindle_random(tno, params):
	db.set_pocket(tno, 0)
	db.set_spindle_tool(tno)
	update_hal_pins(db.get(tno))


def user_unload_spindle_random(tno, params):
	_, pocket = _parse_tp(params)
	if pocket is not None:
		db.set_pocket(tno, pocket)
	db.set_spindle_tool(0)
	update_hal_pins(db.get(0))


# =====================================================================
# наработка (S) — периодический опрос HAL: каждый вызов делает работу
# и сам ставит следующий разовый таймер
# =====================================================================
def runtime_tick():
	global runtime_timer
	spindle_tool = db.get_spindle_tool()
	if spindle_tool and hal is not None:
		try:
			motion_type = hal.get_value("motion.motion-type")
			spindle_on = bool(hal.get_value("spindle.0.on"))
		except Exception:
			motion_type, spindle_on = 0, False
		if spindle_on and motion_type in CUTTING_MOTION_TYPES:
			db.add_runtime(spindle_tool, 1.0)
			if halcomp is not None:
				t = db.get(spindle_tool)
				if t:
					halcomp['s'] = t.s

	runtime_timer = threading.Timer(1.0, runtime_tick)
	runtime_timer.daemon = True
	runtime_timer.start()


# =====================================================================
# start
# =====================================================================
msg("starting, DB=%s, random_tc=%s" % (DB_PATH, RANDOM_TC))
msg("loaded tools: %s" % db.all_tool_numbers())

if RANDOM_TC:
	tooldb_callbacks(user_get_tool, user_put_tool,
					  user_load_spindle_random, user_unload_spindle_random)
else:
	tooldb_callbacks(user_get_tool, user_put_tool,
					  user_load_spindle_nonran, user_unload_spindle_nonran)

tooldb_tools(db.all_tool_numbers())
db._known_set = db.all_tool_numbers()

runtime_timer = None
runtime_tick()

try:
	tooldb_loop()
except (KeyboardInterrupt, BrokenPipeError, EOFError):
	msg("shutdown")
	if runtime_timer is not None:
		runtime_timer.cancel()
	if db._watch_timer is not None:
		db._watch_timer.cancel()
except Exception as e:
	msg("tooldb_loop error: %s" % e)
	if runtime_timer is not None:
		runtime_timer.cancel()
	if db._watch_timer is not None:
		db._watch_timer.cancel()
	raise
