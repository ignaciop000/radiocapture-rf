#!/usr/bin/python

# Copyright 2019,2020 Radiocapture LLC - Radiocapture.com

from gnuradio import gr, analog, filter, blocks
try:
        from gnuradio.gr import firdes
except:
        from gnuradio.filter import firdes


import string
import sys
import time, datetime
import os
import random
import threading
import uuid

#import gnuradio.extras as gr_extras
try:
	import dsd
except:
	pass

from optparse import OptionParser
from gnuradio.eng_option import eng_option
from math import pi
#from gnuradio import repeater, op25
import op25
import op25_repeater as repeater

#from ID3 import *

class file_to_wav(gr.top_block):
	def __init__(self,infile, outfile, input_rate, channel_rate, codec_provoice, codec_p25, sslevel, svlevel):
		gr.top_block.__init__(self, "Top Block")
		
		self.input_rate = input_rate
		self.channel_rate = channel_rate
		
		self.source = blocks.file_source(gr.sizeof_gr_complex*1, infile, False)
		self.lp1_decim = int(input_rate/(channel_rate*1.6))
		print self.lp1_decim
		self.lp1 = filter.fir_filter_ccc(self.lp1_decim,firdes.low_pass( 1.0, self.input_rate, (self.channel_rate/2), ((self.channel_rate/2)*0.6), firdes.WIN_HAMMING))

		#self.audiodemod =  gr.quadrature_demod_cf(1)

		audio_pass = (input_rate/self.lp1_decim)*0.25
		audio_stop = audio_pass+2000
		self.audiodemod = analog.fm_demod_cf(channel_rate=(input_rate/self.lp1_decim), audio_decim=1, deviation=15000, audio_pass=audio_pass, audio_stop=audio_stop, gain=8, tau=75e-6)
		
		self.throttle = blocks.throttle(gr.sizeof_gr_complex*1, self.input_rate)

		self.signal_squelch = analog.pwr_squelch_cc(sslevel,0.01, 0, True)
		self.vox_squelch = analog.pwr_squelch_ff(svlevel, 0.0005, 0, True)
		
		self.audiosink = blocks.wavfile_sink(outfile, 1, 8000)

		if codec_provoice:
			self.dsd = dsd.block_ff(dsd.dsd_FRAME_PROVOICE,dsd.dsd_MOD_AUTO_SELECT,1,0,False)
			channel_rate = input_rate/self.lp1_decim
			self.resampler_in = filter.rational_resampler_fff(interpolation=48000, decimation=channel_rate, taps=None, fractional_bw=None, )
			output_rate = 8000
			resampler = filter.rational_resampler_fff(
                                        interpolation=(input_rate/self.lp1_decim),
                                        decimation=output_rate,
                                        taps=None,
                                        fractional_bw=None,
                                )
		elif codec_p25:
			symbol_deviation = 600.0
			symbol_rate = 4800
			channel_rate = input_rate/self.lp1_decim
			
		        fm_demod_gain = channel_rate / (2.0 * pi * symbol_deviation)
		        fm_demod = analog.quadrature_demod_cf(fm_demod_gain)

		        symbol_decim = 1
		        samples_per_symbol = channel_rate // symbol_rate
		        symbol_coeffs = (1.0/samples_per_symbol,) * samples_per_symbol
		        symbol_filter = filter.fir_filter_fff(symbol_decim, symbol_coeffs)

		        autotuneq = gr.msg_queue(2)
		        demod_fsk4 = op25.fsk4_demod_ff(autotuneq, channel_rate, symbol_rate)


		        # symbol slicer
		        levels = [ -2.0, 0.0, 2.0, 4.0 ]
		        slicer = op25.fsk4_slicer_fb(levels)

			imbe = repeater.vocoder(False, True, 0, "", 0, False)
			self.decodequeue = decodequeue = gr.msg_queue(10000)
			decoder = repeater.p25_frame_assembler('', 0, 0, True, True, False, decodequeue)
	
		        float_conversion = blocks.short_to_float(1, 8192)
		        resampler = filter.rational_resampler_fff(
		                        interpolation=8000,
		                        decimation=8000,
		                        taps=None,
		                        fractional_bw=None,
		                )
	
					
		#Tone squelch, custom GRC block that rips off CTCSS squelch to detect 4800 hz tone and latch squelch after that
		if not codec_provoice and not codec_p25:
			#self.tone_squelch = gr.tone_squelch_ff(audiorate, 4800.0, 0.05, 300, 0, True)
			#tone squelch is EDACS ONLY
			self.high_pass = filter.fir_filter_fff(1, firdes.high_pass(1, (input_rate/self.lp1_decim), 300, 30, firdes.WIN_HAMMING, 6.76))
			#output_rate = channel_rate
			resampler = filter.rational_resampler_fff(
                                        interpolation=8000,
                                        decimation=(input_rate/self.lp1_decim),
                                        taps=None,
                                        fractional_bw=None,
			)
		if(codec_provoice):
			self.connect(self.source, self.throttle, self.lp1, self.audiodemod, self.resampler_in, self.dsd, self.audiosink)
		elif(codec_p25):
			self.connect(self.source, self.throttle, self.lp1, fm_demod, symbol_filter, demod_fsk4, slicer, decoder, imbe, float_conversion, resampler, self.audiosink)
		else:
			self.connect(self.source, self.throttle, self.lp1, self.signal_squelch, self.audiodemod, self.high_pass, self.vox_squelch, resampler, self.audiosink)

		self.time_open = time.time()
		self.time_tone = 0
		self.time_activity = 0
        def upload_and_cleanup(self, filename, time_open, uuid, cdr, filepath):
		if(time_open == 0): raise RuntimeError("upload_and_cleanup() with time_open == 0")
		
		time.sleep(2)
                os.system('nice -n 19 lame --silent ' + filename + ' 2>&1 >/dev/null')
		try:
                	os.makedirs('/nfs/%s' % (filepath, ))
                except:
                        pass
		#try:
		#	id3info = ID3('%s' % (filepath + uuid + '.mp3', ))
		#	id3info['TITLE'] = str(cdr['type']) + ' ' + str(cdr['system_group_local'])
		#	id3info['ARTIST'] = str(cdr['system_user_local'])
		#	id3info['ALBUM'] = str(cdr['system'])
		#	id3info['COMMENT'] = '%s,%s' % (cdr['system_frequency'],cdr['timestamp'])
		#	id3info.write()
		#except InvalidTagError, msg:
		#	print "Invalid ID3 tag:", msg
		now = datetime.datetime.now()
		#try:
		#        os.remove(filename)
		#except:
		#	print 'Error removing ' + filename
	def close(self, upload=True, emergency=False):
		if(not self.in_use): raise RuntimeError('attempted to close() a logging receiver not in_use')
		print "(%s) %s %s" %(time.time(), "Close ", str(self.cdr))

		self.mute()
		if(self.audio_capture):
			self.audiosink.close()

			if(upload):
				_thread_0 = threading.Thread(target=self.upload_and_cleanup,args=[self.filename, self.time_open, self.uuid, self.cdr, self.filepath])
	        		_thread_0.daemon = True
			        _thread_0.start()
			else:
				try:
					os.remove(self.filename)
				except:
					print 'Error removing ' +self.filename

		self.set_tgid(0)
		self.call_id = 0
		self.time_open = 0
		self.in_use = False
		self.uuid =''
		self.cdr = {}
if __name__ == '__main__':
	parser = OptionParser(option_class=eng_option)
        parser.add_option("-i", "--input", dest="input_file", help="Input Filename [.dat]")
        parser.add_option("-o", "--output", dest="output_file", help="Output Filename [.wav]")
	parser.add_option("-r", "--input_rate", dest="rate", help="Audio Rate (in samples/sec)")
	parser.add_option("-c", "--channel_rate", dest="channel_rate", help="Audio Rate (in samples/sec)")
	parser.add_option("-p", "--provoice", dest="codec_provoice", action="store_true", default=False, help="ProVoice decoding (DSD)")
	parser.add_option("-5", "--p25", dest="codec_p25", action="store_true", default=False, help="P25 Codec decoding (DSD)")
	parser.add_option("-s", "--signal_squelch", dest='sslevel', default=-50, help='Signal Squelch level (dB)')
	parser.add_option('-v', '--vox_squelch', dest='svlevel', default=-50, help='Vox Squelch level (dB)')
        (options, args) = parser.parse_args()

        if len(args) != 0 or options.input_file == None:
                parser.print_help()
                sys.exit(1)

	if options.output_file == None:
		if options.input_file[-4:] == '.dat':
			options.output_file = options.input_file[:-4] + ".wav"
		else:
			options.output_file = options.input_file + ".wav"
	if options.rate == None:
		options.rate = 100000
	else:
		options.rate = int(options.rate)
	if options.channel_rate == None:
		options.channel_rate = 12500
	else:
		options.channel_rate = int(options.channel_rate)

	options.sslevel = int(options.sslevel)
	options.svlevel = int(options.svlevel)
        tb = file_to_wav(options.input_file, options.output_file, options.rate, options.channel_rate, options.codec_provoice, options.codec_p25, options.sslevel, options.svlevel)
        tb.start()
	tb.wait()
	time.sleep(1)
	tb.audiosink.close()
