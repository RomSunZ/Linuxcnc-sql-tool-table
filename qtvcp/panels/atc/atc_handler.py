#!/usr/bin/env python3

############################
# **** IMPORT SECTION **** #
############################

import os
import linuxcnc
from qtvcp.core import Status, Info, Tool, Qhal
from qtvcp import logger
from PyQt5 import QtCore, QtGui
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import QPushButton, QVBoxLayout, QHBoxLayout, QLabel
from qtvcp.widgets.led_widget import LED
from qtvcp.widgets.status_label import StatusLabel
from tool_db_client import ToolDBClient
from tool_table_model_ext import (
    ToolTableModelExt, patch_tool_singleton_for_db_program,
    bind_ext_show_selection, bind_ext_add_tool,
    arrange_columns, install_tooltable_number_editors,
)

#import inspect
#from gcodedisplayfixed import GcodeDisplayFixed

###########################################
# **** instantiate libraries section **** #
###########################################

STATUS = Status()
INFO = Info()
QHAL = Qhal()
TOOL = Tool()

# Set up logging
log = logger.getLogger(__name__)
# Set the log level for this module
log.setLevel(logger.DEBUG) # One of DEBUG, INFO, WARNING, ERROR, CRITICAL

###################################
# **** HANDLER CLASS SECTION **** #
###################################

class HandlerClass:

	########################
	# **** INITIALIZE **** #
	########################
	# widgets allows access to  widgets from the QtVCP files
	# at this point the widgets and hal pins are not instantiated
	def __init__(self, halcomp,widgets,paths):
		self.h = halcomp
		self.w = widgets
		self.PATHS = paths
							
		self._scs_label = None
		self._current_rpm = 0.0
		self._scs_tool_number = -1
		self._scs_diameter = 0.0
		self.tool_db_client = None
				
		self.main_led_tool_in_spindle = None
		self.main_led_drawbar = None
		self.main_led_airseal = None
		self._lbl_tool_r_diam = None
		
	##########################################
	# SPECIAL FUNCTIONS SECTION              #
	##########################################

	# at this point:
	# the widgets are instantiated.
	# the HAL pins are built but HAL is not set ready
	# This is where you make HAL pins or initialize state of widgets etc
	def initialized__(self):
		log.debug('INIT qtvcp handler')
		self.init_pins()
		
		STATUS.connect('actual-spindle-speed-changed', self.spindle_speed_changed)
		STATUS.connect('tool-in-spindle-changed', self.on_tool_in_spindle_changed)
		STATUS.connect('tool-info-changed', self.on_tool_info_changed)
		
		QTimer.singleShot(100,self.init_main_ui)
		
	#---------------------------------------------------------------
	def init_main_ui(self):
		if hasattr(self.w, 'MAIN') and self.w.MAIN is not None:		
			self.dragon_custom_tooltable()
			self.dragon_custom_tool_wdgt()
			self.dragon_fix_surface_speed()
		else:
			log.debug("ERROR: ATC tab run in stand alone mode")
			
			
	#---------------------------------------------------------------			
	def dragon_fix_surface_speed(self):
		main = self.w.MAIN
		old_label = main.lbl_tool_scs
		parent_widget = old_label.parentWidget()
		layout = parent_widget.layout()
		index = layout.indexOf(old_label)
		stretch = layout.stretch(index)
		alignment = layout.itemAt(index).alignment() if layout.itemAt(index) else Qt.Alignment()
	
		new_label = StatusLabel(parent_widget)
		new_label.setFrameShape(old_label.frameShape())
		new_label.setFrameShadow(old_label.frameShadow())
		new_label.setAlignment(old_label.alignment())
		new_label.setMinimumSize(old_label.minimumSize())
		new_label.setMaximumSize(old_label.maximumSize())
		new_label.setSizePolicy(old_label.sizePolicy()) 
		new_label.setText('0')
		
		layout.replaceWidget(old_label, new_label)
		layout.setStretch(index, stretch)
		if alignment:
			layout.setAlignment(new_label, alignment)
			
		old_label._set_alt_text = lambda *a, **kw: None
		old_label._set_text = lambda *a, **kw: None
		old_label._set_surface_speed = lambda *a, **kw: None
		old_label._ss_tool_diam = lambda *a, **kw: None
		old_label.setText = lambda *a, **kw: None	
		old_label._ss_spindle_speed = lambda *a, **kw: None	
		old_label._set_work_diameter = lambda *a, **kw: None	
		old_label._switch_units = lambda *a, **kw: None	
		old_label.hide()		
		old_label.setParent(None)
		old_label.deleteLater()
	
		main.lbl_tool_scs = new_label
		self._scs_label = new_label
							
	#---------------------------------------------------------------
	def dragon_custom_tooltable(self):
		if hasattr(self.w.MAIN, 'tooloffsetview'):
			self.tool_db_client = ToolDBClient(self.get_tooldb_sock_path())
			self.toolTableWidget = self.w.MAIN.tooloffsetview	
			if self.toolTableWidget is not None:
				# переопределяем поведение singleton TOOL — не класс _TStat целиком
				patch_tool_singleton_for_db_program(TOOL, self.tool_db_client)

				# подменяем модель конкретного виджета на наш подкласс
				ext_model = ToolTableModelExt(self.toolTableWidget, self.tool_db_client)
				self.toolTableWidget.tablemodel = ext_model
				self.toolTableWidget.setModel(ext_model)
				ext_model.update(TOOL.GET_TOOL_MODELS())   # немедленное первичное наполнение

				# showSelection — на этом же экземпляре, не на классе ToolOffsetView
				bind_ext_show_selection(self.toolTableWidget)
				
				# add_tool — тоже на этом же экземпляре: без этого кнопка
				# ADD по-прежнему дёргает штатный add_tool(), который
				# дублирует параметры текущей строки вместо создания
				# нового инструмента с номером max+1 и пустыми полями
				bind_ext_add_tool(self.toolTableWidget, self.tool_db_client)
				
				install_tooltable_number_editors(self.toolTableWidget)
				arrange_columns(self.toolTableWidget, ext_model)
				
		# add button <Probe Tool>		
		if hasattr(self.w.MAIN, 'widget_tool_table'):
			container = self.w.MAIN.widget_tool_table
			layout = container.layout()
			
			if layout is not None:
				#tool change button
				self.btnChangeTool = QPushButton("Tn M6")
				self.btnChangeTool.setMinimumHeight(50)
				self.btnChangeTool.setMaximumHeight(50)
				self.btnChangeTool.clicked.connect(self.btnChangeTool_clicked)					
				cnt = layout.count()
				if (cnt > 0):
					layout.insertWidget(cnt - 1, self.btnChangeTool)
				else:
					layout.addWidget(self.btnChangeTool)
				
				#probe tool button	
				self.btnProbeTool = QPushButton("Probe\nTool")
				self.btnProbeTool.setMinimumHeight(50)
				self.btnProbeTool.setMaximumHeight(50)
				self.btnProbeTool.clicked.connect(self.btnProbeTool_clicked)					
				cnt = layout.count()
				if (cnt > 0):
					layout.insertWidget(cnt - 1, self.btnProbeTool)
				else:
					layout.addWidget(self.btnProbeTool)
			else:
				log.debug("ERROR: Widget 'widget_tool_table' not found")	
		
		"""		
		#change offset table		
		if hasattr(self.w.MAIN, 'offset_table'):
			container = self.w.MAIN.widget_tool_table
			table = self.w.MAIN.findChild(OriginOffsetView, 'offset_table')
			table.setProperty('metric_template', '%10.5f')
		else:
			log.debug("ERROR: Widget 'offset_table' not found")	
		"""
	
	#---------------------------------------------------------------
	def dragon_custom_tool_wdgt(self):
			if hasattr(self.w.MAIN, 'frame_tool'):
				layout = self.w.MAIN.frame_tool.layout()
				if layout is not None:
					lbl_img = None
					if hasattr(self.w.MAIN, 'lbl_tool_image'):
						lbl_img = self.w.MAIN.lbl_tool_image
						
					self.main_led_tool_in_spindle = LED()
					self.main_led_drawbar = LED()
					self.main_led_airseal = LED()
					
					rows = [
							("SPINDLE AIR SEAL", self.main_led_airseal),
							("DRAWBAR RELEASED", self.main_led_drawbar),
							("TOOL IN SPINDLE", self.main_led_tool_in_spindle),]
							
					status_layout = QVBoxLayout()
					status_layout.setContentsMargins(0, 0, 0, 0)
					status_layout.setSpacing(2) 
							
					if lbl_img is not None:
						insert_index = layout.indexOf(lbl_img) + 1
						pix = lbl_img.pixmap()
						if pix is not None and not pix.isNull():
							new_w = int(pix.width() * 0.6)
							new_h = int(pix.height() * 0.6)
							scaled_pix = pix.scaled(new_w, new_h, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
							lbl_img.setPixmap(scaled_pix)
						
					else:
						insert_index = layout.count() - 1
						
					for text, led in reversed(rows):
						led.setMinimumSize(24, 24)
						led.setMaximumSize(24, 24)
						led.setProperty('color', QtGui.QColor(0, 255, 0))
						led.setProperty('off_color', QtGui.QColor(255, 0, 0))
						try:
							led.setProperty('border_color', QtGui.QColor(0, 0, 0))
						except Exception:
							pass
						
						row = QHBoxLayout()
						row.addWidget(QLabel(text))
						row.addWidget(led)
						status_layout.addLayout(row)
					layout.insertLayout(insert_index, status_layout)	
					
					self.main_led_tool_in_spindle.setState(bool(self.pin_tool_state.get()))
					self.main_led_drawbar.setState(bool(self.pin_drawbar_state.get()))
					self.main_led_airseal.setState(bool(self.pin_airseal_state.get()))

					# label_2 «DIAMETER» → «D WEAR» (рядом lbl_tool_diameter = D/износ)
					# над ней: DIAMETER + R из нашей таблицы
					self._setup_diameter_labels()
				else:
					log.debug("ERROR: 'frame_tool' has no layout")
			else:
				log.debug("ERROR: Widget 'frame_tool' not found")				
		
	#---------------------------------------------------------------
	def _setup_diameter_labels(self):
		"""В frame_tool: label_2 DIAMETER→D WEAR; сверху строка DIAMETER + R."""
		main = self.w.MAIN
		if main is None:
			return
		if getattr(self, '_lbl_tool_r_diam', None) is not None:
			return

		lbl_cap = getattr(main, 'label_2', None)
		if lbl_cap is not None:
			try:
				if str(lbl_cap.text()).strip().upper().startswith('DIAMETER'):
					lbl_cap.setText('D WEAR')
			except Exception:
				pass

		wear_val = getattr(main, 'lbl_tool_diameter', None)
		if wear_val is None:
			return

		# В .ui: VBox → item HBox(label_2, lbl_tool_diameter)
		parent = wear_val.parentWidget()
		if parent is None:
			return
		vbox = parent.layout()
		if vbox is None:
			log.debug('_setup_diameter_labels: no parent layout')
			return

		insert_at = -1
		for i in range(vbox.count()):
			item = vbox.itemAt(i)
			if item is None:
				continue
			lay = item.layout()
			if lay is None:
				continue
			for j in range(lay.count()):
				it = lay.itemAt(j)
				if it is not None and it.widget() is wear_val:
					insert_at = i
					break
			if insert_at >= 0:
				break
		if insert_at < 0:
			log.debug('_setup_diameter_labels: diameter row not found')
			return

		row = QHBoxLayout()
		row.setSpacing(4)
		cap = QLabel('DIAMETER')
		cap.setMinimumHeight(30)
		cap.setMaximumHeight(30)
		cap.setIndent(4)
		if lbl_cap is not None:
			try:
				cap.setSizePolicy(lbl_cap.sizePolicy())
			except Exception:
				pass
		val = StatusLabel()
		val.setObjectName('lbl_tool_r_diameter')
		val.setMinimumSize(60, 30)
		val.setMaximumSize(60, 30)
		val.setAlignment(Qt.AlignCenter)
		try:
			val.setFrameShape(wear_val.frameShape())
			val.setFrameShadow(wear_val.frameShadow())
		except Exception:
			pass
		val.setText('0')
		row.addWidget(cap)
		row.addWidget(val)
		vbox.insertLayout(insert_at, row)
		self._lbl_tool_r_diam = val
		log.debug('_setup_diameter_labels: DIAMETER(R) above D WEAR')

		# начальное значение, если инструмент уже известен
		try:
			self._set_r_diameter_text(int(STATUS.get_current_tool() or 0))
		except Exception:
			pass

	#---------------------------------------------------------------
	def _set_r_diameter_text(self, toolnum):
		"""Значение DIAMETER = R из нашей БД/таблицы."""
		if self._lbl_tool_r_diam is None:
			return
		r = 0.0
		try:
			tno = int(toolnum or 0)
		except (TypeError, ValueError):
			tno = 0
		if tno > 0:
			# предпочтительно демон (поле r)
			if self.tool_db_client is not None:
				try:
					rec = self.tool_db_client.get_tool(tno) or {}
					r = float(rec.get('r', rec.get('R', 0.0)) or 0.0)
				except Exception:
					r = self.get_tool_diameter(tno)
			else:
				r = self.get_tool_diameter(tno)
		try:
			if INFO.MACHINE_IS_METRIC:
				self._lbl_tool_r_diam.setText('{:.3f}'.format(r))
			else:
				self._lbl_tool_r_diam.setText('{:.4f}'.format(r))
		except Exception:
			self._lbl_tool_r_diam.setText(str(r))

	#---------------------------------------------------------------
	def init_pins(self):
		# spindle control pins
		self.pin_drawbar = QHAL.newpin("spindle-drawbar", QHAL.HAL_BIT, QHAL.HAL_OUT)
		self.pin_drawbar_state = QHAL.newpin("spindle-drawbar-state", QHAL.HAL_BIT, QHAL.HAL_IN)
		self.pin_drawbar_state.value_changed.connect(self.spindle_clamp_state_changed)
		
		self.pin_tool_state = QHAL.newpin("spindle-tool-state", QHAL.HAL_BIT, QHAL.HAL_IN)
		self.pin_tool_state.value_changed.connect(self.spindle_tool_state_changed)
		
		self.pin_airseal_state = QHAL.newpin("spindle-air-seal-state", QHAL.HAL_BIT, QHAL.HAL_IN)
		self.pin_airseal_state.value_changed.connect(self.spindle_air_seal_changed)
		
	########################
	# callbacks from STATUS #
	########################
	
	def _restore_spindle_tool_from_db(self):
		"""После первого all-homed: M61 из spindle_tool демона, если
		chk_reload_tool (как дефолтный Dragon с Reload tool).
		"""
		if not getattr(self, '_spindle_tool_restore_pending', False):
			return
		self._spindle_tool_restore_pending = False

		chk = None
		try:
			if hasattr(self.w, 'MAIN') and self.w.MAIN is not None:
				chk = getattr(self.w.MAIN, 'chk_reload_tool', None)
		except Exception:
			chk = None
		if chk is None or not chk.isChecked():
			log.debug('restore_spindle_tool: Reload tool off, skip')
			return

		tno = 0
		if self.tool_db_client is None:
			log.debug('restore_spindle_tool: no db client yet')
			return
		try:
			resp = self.tool_db_client.list_tools()
			if resp and resp.get('ok'):
				tno = int(resp.get('spindle_tool') or 0)
		except Exception as e:
			log.debug('restore_spindle_tool list_tools: {}'.format(e))
			return

		try:
			cur = int(STATUS.get_current_tool() or 0)
		except Exception:
			cur = 0

		if tno <= 0:
			log.debug('restore_spindle_tool: DB spindle_tool=0, skip')
			return
		if cur == tno:
			log.debug('restore_spindle_tool: already T{}'.format(tno))
			return

		log.debug('restore_spindle_tool: M61 Q{} G43 (was T{})'.format(tno, cur))
		try:
			ACTION.CALL_MDI("M61 Q{} G43".format(tno))
		except Exception as e:
			log.error('restore_spindle_tool M61 failed: {}'.format(e))
		
	#---------------------------------------------------------------
	def spindle_clamp_state_changed(self, s):
		if self.main_led_drawbar is not None:
			self.main_led_drawbar.setState(s)
		
	#---------------------------------------------------------------
	def spindle_tool_state_changed(self, s):
		if self.main_led_tool_in_spindle is not None:
			self.main_led_tool_in_spindle.setState(s)
		
	#---------------------------------------------------------------
	def spindle_air_seal_changed(self, s):
		if self.main_led_airseal is not None:
			self.main_led_airseal.setState(s)
		
	#---------------------------------------------------------------
	def spindle_speed_changed(self, w, data):
		self._current_rpm = abs(data)
		self.render_surface_cut_speed()
		
	#---------------------------------------------------------------
	def on_tool_in_spindle_changed(self, w, data):
		# data = номер инструмента в шпинделе (не RPM)
		try:
			tno = int(data or 0)
		except (TypeError, ValueError):
			tno = 0
		self._scs_tool_number = tno
		self._set_r_diameter_text(tno)
		self.render_surface_cut_speed()

	def on_tool_info_changed(self, w, data):
		tno = 0
		if data is not None:
			tno = int(getattr(data, 'id', 0) or 0)
		if not tno:
			try:
				tno = int(STATUS.get_current_tool() or 0)
			except Exception:
				tno = 0				
		if tno == self._scs_tool_number:
			self._scs_diameter = self.get_tool_diameter(self._scs_tool_number)
			self._set_r_diameter_text(self._scs_tool_number)
			self.render_surface_cut_speed()

	#######################
	# CALLBACKS FROM FORM #
	#######################

	def btnChangeTool_clicked(self):
		handler = getattr(self.w.MAIN, "HANDLER", None)
		bSend = False
		if handler and hasattr(handler, "add_status"):
			bSend = True
			log.debug('ATC add_status found')
			
		checked = self.toolTableWidget.get_checked_list()
		if len(checked) > 1:
			if bSend:
				handler.add_status("Select only 1 tool to load", CRITICAL)
		elif checked:
			if bSend:
				handler.add_status("Loaded tool {}".format(checked[0]))
				
			ACTION.CALL_MDI("T{} M6".format(checked[0]))
		else:
			if bSend:
				handler.add_status("No tool selected", WARNING)			
	
    #---------------------------------------------------------------
	def btnProbeTool_clicked(self):
		ACTION.CALL_MDI("o<tool_length_measure> call")
	
	#####################
	# general functions #
	#####################
    #---------------------------------------------------------------
	def get_tooldb_sock_path(self):
		ini_path = os.environ.get("INI_FILE_NAME")
		if not ini_path:
			return None

		config_dir = os.path.dirname(os.path.abspath(ini_path))
		ini = linuxcnc.ini(ini_path)
		db_prog = (ini.find("EMCIO", "DB_PROGRAM") or "").split()

		if len(db_prog) < 2:
			# fallback, если DB_PROGRAM без аргумента
			db_file = os.path.join(config_dir, "tool_db.sql")
		else:
			db_file = db_prog[1]
			if not os.path.isabs(db_file):
				db_file = os.path.join(config_dir, db_file)

		db_path = os.path.abspath(db_file)
		sock_path = db_path + ".sock" 
		return sock_path
		
    #---------------------------------------------------------------

	def get_tool_diameter(self, tool_num):
		if not tool_num:
			return 0.0
		if self.tool_db_client is not None:
			try:
				rec = self.tool_db_client.get_tool(int(tool_num)) or {}
				return float(rec.get('r', rec.get('R', 0.0)) or 0.0)
			except Exception:
				pass
		tool_data = self.get_tool_data(tool_num)
		if tool_data is None:
			return 0.0
		try:
			return float(tool_data.get('Diameter') or 0.0)
		except (TypeError, ValueError):
			return 0.0
    #---------------------------------------------------------------
	def conversion(self, data):
		if INFO.MACHINE_IS_METRIC :
			return INFO.convert_machine_to_metric(data)
		else:
			return INFO.convert_machine_to_imperial(data)

    #---------------------------------------------------------------
	def render_surface_cut_speed(self):
		if self._scs_label is None:
			return
			
		if not self._scs_diameter:
			self._scs_label.setText('0')
			return
		
		diam = self.conversion(self._scs_diameter)	
		circ = abs(3.14 * self._current_rpm * diam)
		
		if INFO.MACHINE_IS_METRIC:
			scs = circ/1000 # meters per minute
		else:
			scs = circ/12 # feet per minute
		log.debug(f"---------diam={diam} current_rpm={self._current_rpm} scs={scs}")	
		self._scs_label.setText('{:.0f}'.format(scs))
    #---------------------------------------------------------------

	def get_tool_data(self, tool_number):
		if self.toolTableWidget is not None:
			model = self.toolTableWidget.model()
			if model is None or not hasattr(model, "arraydata"):
				print("Модель таблицы не имеет arraydata")
				log.debug('Модель таблицы не имеет arraydata')
				return None
				
			if hasattr(model, "headerdata"):
				headers = model.headerdata 
			else:
				headers = [f"col{c}" for c in range(len(model.arraydata[row]))]
			#dict_keys(['', 'tool', 'pocket', 'X', 'X Wear', 'Y', 'Y Wear', 'Z', 'Z Wear', 'A', 'B', 'C', 'U', 'V', 'W', 'D Wear', 'Heavy', 'Diameter', 'Orient', 'Comment'])
			tool_idx = 1
			for l in headers:
				if 'tool' in l:
					tool_idx = l
				break

			for row, row_data in enumerate(model.arraydata):
				# Предположим, что первый элемент в row_data — номер инструмента (T)
				if len(row_data) == 0:
					continue
				
				if int(row_data[tool_idx]) == int(tool_number):						
					data = {header: row_data[i] if i < len(row_data) else None
							for i, header in enumerate(headers)}
					return data
		else:
			return None
	
	def get_tool_remark(self, tool_table, tool_num):
		for l in tool_table:
			if (f'T{tool_num} ') in l:
				tool_entry = l
				break
		tool_remark = self.after(tool_entry,";")
		return tool_remark		
	
	#####################
	# KEY BINDING CALLS #
	#####################
	
	def eventFilter(self, obj, event):
		if event.type() == QtCore.QEvent.KeyPress:
			if event.key() == Qt.Key_F11:
				on_keycall_F11()
				event.accept()
				return True
		return super().eventFilter(obj, event)
	
	def on_key(self, event):
		if event.key() == QtCore.Qt.Key_F11:
			log.debug('on_keycall_F11')	
			if self.mainBtnJogRate is not None:
				self.mainBtnJogRate.click()
			event.accept()
		else:
			super(QtWidgets.QWidget, self.key_widget).keyPressEvent(event)		
		
	def on_keycall_F11(self):
		log.debug('on_keycall_F11')	
		if self.mainBtnJogRate is not None:
			self.mainBtnJogRate.click()
					
            
	###########################
	# **** closing event **** #
	###########################
   
	def closing_cleanup__(self):
		log.debug('CLOSE')
		
	##############################
	# required class boiler code #
	##############################

	def __getitem__(self, item):
		return getattr(self, item) 
	def __setitem__(self, item, value):
		return setattr(self, item, value)

################################
# required handler boiler code #
################################

def get_handlers(halcomp,widgets,paths):
	 return [HandlerClass(halcomp,widgets,paths)]

