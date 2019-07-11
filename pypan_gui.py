import os
import numpy as np
import soundfile as sf
from vispy import scene, color
from PyQt5 import QtGui, QtCore, QtWidgets
from scipy import interpolate

#custom modules
from util import vispy_ext, fourier, spectrum, resampling, wow_detection, snd, widgets, io_ops, units, markers

class ObjectWidget(QtWidgets.QWidget):
	"""
	Widget for editing OBJECT parameters
	"""

	def __init__(self, parent=None):
		super(ObjectWidget, self).__init__(parent)
		
		self.parent = parent
		
		self.filename = ""
		self.deltraces = []
		
		
		self.display_widget = widgets.DisplayWidget(self.parent.canvas)
		self.resampling_widget = widgets.ResamplingWidget()
		self.progress_widget = widgets.ProgressWidget()
		self.audio_widget = snd.AudioWidget()
		self.inspector_widget = widgets.InspectorWidget()
		buttons = [self.display_widget, self.resampling_widget, self.progress_widget, self.audio_widget, self.inspector_widget ]
		widgets.vbox2(self, buttons)
		
		self.parent.canvas.fourier_thread.notifyProgress.connect( self.progress_widget.onProgress )
		
	def open_audio(self):
		#just a wrapper around load_audio so we can access that via drag & drop and button
		#pyqt5 returns a tuple
		filename = QtWidgets.QFileDialog.getOpenFileName(self, 'Open Audio', self.parent.cfg["dir_in"], "Audio files (*.flac *.wav)")[0]
		self.load_audio(filename)
			
	def load_audio(self, filename):
		#ask the user if it should really be opened, if another file was already open
		if widgets.abort_open_new_file(self, filename, self.filename):
			return
		
		try:
			self.parent.canvas.compute_spectra( (filename, filename), self.display_widget.fft_size, self.display_widget.fft_overlap, channels=(0, 1) )
		# file could not be opened
		except RuntimeError as err:
			print(err)
		# no issues, we can continue
		else:
			if self.parent.canvas.channels != 2:
				print("Must be stereo!")
				return
			self.filename = filename
			
			#Cleanup of old data
			self.delete_traces(not_only_selected=True)
			self.resampling_widget.refill(self.parent.canvas.channels)
			
			#read pan curve
			for a0, a1, b0, b1, d in io_ops.read_lag(self.filename):
				markers.PanSample(self.parent.canvas, (a0, a1), (b0, b1), d)
			self.parent.canvas.pan_line.update()
			self.parent.update_file(self.filename)

	def save_traces(self):
		#get the data from the traces and regressions and save it
		io_ops.write_lag(self.filename, [ (lag.a[0], lag.a[1], lag.b[0], lag.b[1], lag.pan) for lag in self.parent.canvas.pan_samples ] )
			
	def delete_traces(self, not_only_selected=False):
		self.deltraces= []
		for trace in reversed(self.parent.canvas.pan_samples):
			if (trace.selected and not not_only_selected) or not_only_selected:
				self.deltraces.append(trace)
		for trace in self.deltraces:
			trace.remove()
		self.parent.canvas.pan_line.update()
		#this means a file was loaded, so clear the undo stack
		if not_only_selected:
			self.deltraces= []
	
	def run_resample(self):
		if self.filename and self.parent.canvas.pan_samples:
			channels = self.resampling_widget.channels
			if channels and self.parent.canvas.pan_samples:
				lag_curve = self.parent.canvas.pan_line.data
				signal, sr, channels = io_ops.read_file(self.filename)
				af = np.interp(np.arange(len(signal[:,0])), lag_curve[:,0]*sr, lag_curve[:,1])
				io_ops.write_file(self.filename, signal[:,1]*af, sr, 1)
					
class MainWindow(widgets.MainWindow):

	def __init__(self):
		widgets.MainWindow.__init__(self, "pypan", ObjectWidget, Canvas)
		mainMenu = self.menuBar() 
		fileMenu = mainMenu.addMenu('File')
		editMenu = mainMenu.addMenu('Edit')
		button_data = ( (fileMenu, "Open", self.props.open_audio, "CTRL+O"), \
						(fileMenu, "Save", self.props.save_traces, "CTRL+S"), \
						(fileMenu, "Resample", self.props.run_resample, "CTRL+R"), \
						(fileMenu, "Exit", self.close, ""), \
						(editMenu, "Delete Selected", self.props.delete_traces, "DEL"), \
						)
		self.add_to_menu(button_data)
		
class Canvas(spectrum.SpectrumCanvas):

	def __init__(self):
		spectrum.SpectrumCanvas.__init__(self, bgcolor="black")
		self.unfreeze()
		self.pan_samples = []
		self.pan_line = markers.PanLine(self)
		self.freeze()
		
	def on_mouse_press(self, event):
		#selection
		b = self.click_spec_conversion(event.pos)
		#are they in spec_view?
		if b is not None:
			self.props.audio_widget.cursor(b[0])
		if event.button == 2:
			closest_lag_sample = self.get_closest( self.pan_samples, event.pos )
			if closest_lag_sample:
				closest_lag_sample.select_handle()
				event.handled = True
	
	def on_mouse_release(self, event):
		#coords of the click on the vispy canvas
		if self.props.filename and (event.trail() is not None) and event.button == 1:
			last_click = event.trail()[0]
			click = event.pos
			if last_click is not None:
				a = self.click_spec_conversion(last_click)
				b = self.click_spec_conversion(click)
				#are they in spec_view?
				if a is not None and b is not None:
					if "Shift" in event.modifiers:
						L = self.fft_storage[ self.keys[0] ]
						R = self.fft_storage[ self.keys[1] ]
						
						t0, t1 = sorted((a[0], b[0]))
						freqs = sorted((a[1], b[1]))
						fL = max(freqs[0], 1)
						fU = min(freqs[1], self.sr//2-1)
						first_fft_i = 0
						num_bins, last_fft_i = L.shape
						#we have specified start and stop times, which is the usual case
						if t0:
							#make sure we force start and stop at the ends!
							first_fft_i = max(first_fft_i, int(t0*self.sr/self.hop)) 
						if t1:
							last_fft_i = min(last_fft_i, int(t1*self.sr/self.hop))

						def freq2bin(f): return max(1, min(num_bins-3, int(round(f * self.fft_size / self.sr))) )
						bL = freq2bin(fL)
						bU = freq2bin(fU)
						
						# dBs = np.nanmean(units.to_dB(L[bL:bU,first_fft_i:last_fft_i])-units.to_dB(R[bL:bU,first_fft_i:last_fft_i]), axis=0)
						# fac = units.to_fac(dBs)
						# out_times = np.arange(first_fft_i, last_fft_i)*hop/sr
						# PanSample(self, a, b, np.mean(fac) )
						
						# faster and simpler equivalent avoiding fac - dB - fac conversion
						fac = np.nanmean(L[bL:bU,first_fft_i:last_fft_i] / R[bL:bU,first_fft_i:last_fft_i])
						markers.PanSample(self, a, b, fac )
						self.pan_line.update()
						
			
# -----------------------------------------------------------------------------
if __name__ == '__main__':
	widgets.startup( MainWindow )
