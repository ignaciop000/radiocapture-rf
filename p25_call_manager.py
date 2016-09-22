#!/usr/bin/env python


from stompest.config import StompConfig
from stompest.sync import Stomp
from stompest.protocol import StompSpec

import json
import threading
import time
import uuid
import sys
import signal
import math
import logging
import logging.config

from redis_demod_manager import redis_demod_manager
from client_activemq import client_activemq

class p25_call_manager():
        def __init__(self):
                self.log = logging.getLogger('overseer.p25_call_manager')
                self.log.debug('Initializing p25_call_manager')
		self.demod_type = 'p25'

		self.redis_demod_manager = redis_demod_manager(self)

		self.lock = threading.RLock()

		self.patches = {}
		self.hang_time = 5
		self.instance_metadata = {}
		self.system_metadata = {}
		self.continue_running = True

		self.amq_clients = {}
		self.amq_clients['raw_voice'] = client_activemq(10)
		#self.amq_clients['raw_voice'].subscribe('/topic/raw_voice', self, self.process_raw_control.im_func, False, 'packet_type = \'Group Voice Channel User\' or packet_type = \'Call Termination / Cancellation\' or packet_type = \'Group Voice Channel Update\'')

		periodic_timeout_thread = threading.Thread(target=self.periodic_timeout_thread)
		periodic_timeout_thread.daemon = True
                periodic_timeout_thread.start()

	def notify_demod_new(self, demod_instance_uuid):
		self.log.debug('Notified of new demod %s' % (demod_instance_uuid))
		self.amq_clients[demod_instance_uuid] = client_activemq()
		self.amq_clients[demod_instance_uuid].subscribe('/topic/raw_control/%s' % (demod_instance_uuid), self, self.process_raw_control.im_func, False, 'packet_type = \'GRP_V_CH_GRANT\' or packet_type = \'MOT_PAT_GRP_VOICE_CHAN_GRANT\' or packet_type = \'GRP_V_CH_GRANT_UPDT\' or packet_type = \'MOT_PAT_GRP_VOICE_CHAN_GRANT_UPDT\' or packet_type = \'MOT_PAT_GRP_ADD_CMD\' or packet_type = \'MOT_PAT_GRP_DEL_CMD\' or packet_type = \'IDEN_UP\' or packet_type = \'IDEN_UP_VU\' or packet_type = \'IDEN_UP_TDMA\'')
		self.amq_clients[demod_instance_uuid].subscribe('/topic/raw_voice/%s' % (demod_instance_uuid), self, self.process_raw_control.im_func, False, 'packet_type = \'Group Voice Channel User\' or packet_type = \'Call Termination / Cancellation\' or packet_type = \'Group Voice Channel Update\'')

	def notify_demod_expire(self, demod_instance_uuid):
		self.log.debug('Notified of expired demod %s' % (demod_instance_uuid))
		if demod_instance_uuid in self.amq_clients:
			self.amq_clients[demod_instance_uuid].unsubscribe('/topic/raw_control/%s' % (demod_instance_uuid))

	def get_channel_detail(self, instance, channel):
                chan_ident = (channel & 0xf000)>>12
                chan_number = channel & 0x0fff
                try:
                        base_freq = self.instance_metadata[instance]['channel_identifier_table'][chan_ident]['Base Frequency']
                        chan_spacing = self.instance_metadata[instance]['channel_identifier_table'][chan_ident]['Channel Spacing']/1000
                        slots = self.instance_metadata[instance]['channel_identifier_table'][chan_ident]['Slots']
			modulation = self.instance_metadata[instance]['channel_identifier_table'][chan_ident]['Type']
                except KeyError:
                        return False, False, False, False
                chan_freq = ((chan_number/slots)*chan_spacing)
                slot_number = (chan_number % slots)
                channel_frequency = math.floor((base_freq + chan_freq)*1000000)
                channel_bandwidth = self.instance_metadata[instance]['channel_identifier_table'][chan_ident]['BW']*1000

                return channel_frequency, channel_bandwidth, slot_number, modulation

	def get_system_from_instance(self, instance_uuid):
		try:
			return self.redis_demod_manager.demods[instance_uuid]['system_uuid']
		except:
			return False

	def close_call(self, instance_uuid, call_uuid):
		system_uuid = self.get_system_from_instance(instance_uuid)
		sct = self.system_metadata[system_uuid]['call_table']
                ict = self.instance_metadata[instance_uuid]['call_table']

		if call_uuid not in ict:
			return #Cant close a call thats not open
		
        	self.amq_clients['raw_voice'].send_event_lazy('/queue/call_management/timeout', {'call_uuid': call_uuid, 'instance_uuid': instance_uuid})
                self.log.info('Closing call due to close_call(): %s %s' % (instance_uuid, call_uuid))
		with self.lock:
	                del ict[call_uuid]
        	        del sct[call_uuid]['instances'][instance_uuid]
                	if len(sct[call_uuid]['instances']) == 0:
	                	del sct[call_uuid]
	def call_continuation(self, instance_uuid, channel, group_address):
                channel_frequency, channel_bandwidth, slot, modulation = self.get_channel_detail(instance_uuid, channel)

                if channel_frequency == False:
                        return False

                system_uuid = self.get_system_from_instance(instance_uuid)
                if system_uuid == False:
                        return False

                sct = self.system_metadata[system_uuid]['call_table']
                ict = self.instance_metadata[instance_uuid]['call_table']

                closed_calls = []

	        for call in ict.keys():
        		if ict[call]['system_channel_local'] == channel and ict[call]['system_group_local'] == group_address:
                		ict[call]['time_activity'] = time.time()
                                return True

	def call_user_to_group(self, instance_uuid, channel, group_address, user_address=0):
		channel_frequency, channel_bandwidth, slot, modulation = self.get_channel_detail(instance_uuid, channel)

		if channel_frequency == False:
			return False

		system_uuid = self.get_system_from_instance(instance_uuid)
		if system_uuid == False:
			return False

		sct = self.system_metadata[system_uuid]['call_table']
		ict = self.instance_metadata[instance_uuid]['call_table']

		closed_calls = []
		for call in ict.keys():
			if ict[call]['system_channel_local'] == channel and ict[call]['system_group_local'] == group_address and (user_address == 0 or ict[call]['system_user_local'] == user_address):
				ict[call]['time_activity'] = time.time()
				return True

			if ict[call]['system_channel_local'] == channel and ict[call]['system_group_local'] != group_address:
				#different group, kill existing
				closed_calls.append(call)
			if ict[call]['system_channel_local'] == channel and ict[call]['system_group_local'] == group_address and user_address != 0 and ict[call]['system_user_local'] != 0 and ict[call]['system_user_local'] != user_address:
				#different user on same group, and neither new or old user = 0, kill existing
				closed_calls.append(call)


		for call_uuid in closed_calls:
			self.close_call(instance_uuid, call_uuid)
			
		#Not a continuation, new call
		call_uuid = None
		call_count = 0
		for call in sct.keys():
			if sct[call]['system_group_local'] == group_address and (user_address == 0 or sct[call]['system_user_local'] == user_address) and time.time() - sct[call]['time_open'] < 1:
				call_uuid = sct[call]['call_uuid']
				call_count + 1

		if call_count >= 3:
			pass
			#return False

		if call_uuid == None:
			#call is new systemwide, assign new UUID
			call_uuid = '%s' % uuid.uuid4()

		instance = self.redis_demod_manager.demods[instance_uuid]
		if modulation == 'FDMA' and instance['system_modulation'] == 'C4FM':
			modulation_type = 'p25'
		elif modulation == 'TDMA' and instance['system_modulation'] == 'C4FM':
			modulation_type = 'p25_tdma'
		elif modulation == 'FDMA' and instance['system_modulation'] == 'CQPSK':
			modulation_type = 'p25_cqpsk'
		elif modulation == 'TDMA' and instance['system_modulation'] == 'CQPSK':
			modulation_type = 'p25_cqpsk_tdma'
		else:
			modulation_type = 'ERROR %s %s' % (modulation, instance['system_modulation'])

		patches = []

		#for x in self.instance_metadata[instance_uuid]:
		#	if self.instance_metadata[instance_uuid][x]
			
		cdr = {
			'call_uuid': call_uuid,
	                'system_id': system_uuid,
			'transmit_site_uuid': instance['transmit_site_uuid'],
			'instance_uuid': instance_uuid,
                        'system_group_local': group_address,
                        'system_user_local': user_address,
                        'system_channel_local': channel,
                        'type': 'group',
			'frequency': channel_frequency,
			'channel_bandwidth': channel_bandwidth,
			'modulation_type': modulation_type,
			'slot': slot,
                        'hang_time': self.hang_time,
			'time_open': time.time(),
			'time_activity': time.time(),
			'p25_wacn': instance['site_detail']['WACN ID'],
			'p25_system_id': instance['site_detail']['System ID'],
			
                        }
		
		with self.lock:
			ict[call_uuid] = cdr
			if call_uuid not in sct:
				sct[call_uuid] = cdr
				sct[call_uuid]['instances'] = {instance_uuid: True}
			else:
				sct[call_uuid]['instances'][instance_uuid] = True
		

		#event call open to record subsys
		self.amq_clients['raw_voice'].send_event_lazy('/queue/call_management/new_call', cdr)
		self.redis_demod_manager.publish_call_table(instance_uuid, ict)
		self.log.info('OPEN: %s %s %s %s' % (cdr['instance_uuid'], cdr['call_uuid'], cdr['system_group_local'], cdr['system_user_local']))

	def periodic_timeout_thread(self):
		while self.continue_running:
			time.sleep(0.1)
			for instance in self.instance_metadata.keys():
				ict = self.instance_metadata[instance]['call_table']
				system_uuid = self.get_system_from_instance(instance)
				if system_uuid == False:
					continue
				sct = self.system_metadata[system_uuid]['call_table']

				closed_calls = []
				for call_uuid in ict.keys():
					if time.time()-ict[call_uuid]['time_activity'] > ict[call_uuid]['hang_time']:
						closed_calls.append(call_uuid)
						#event call close to record subsys on call specific queue
						self.amq_clients['raw_voice'].send_event_lazy('/queue/call_management/timeout', {'call_uuid': call_uuid, 'instance_uuid': instance})						
	
						self.log.info('CLOSE: %s' % (ict[call_uuid]))
				for call_uuid in closed_calls:
					del ict[call_uuid]
					del sct[call_uuid]['instances'][instance]
					if len(sct[call_uuid]['instances']) == 0:
						del sct[call_uuid]
				if len(closed_calls) > 0:
					self.redis_demod_manager.publish_call_table(instance, ict)
					
	def process_raw_control(self, t, headers):
				try:
					if 'instance_uuid' in t.keys():
						instance_uuid = t['instance_uuid']
						packet_type = 'voice'
					else:
						instance_uuid = headers['destination'].replace('/topic/raw_control/', '')
						packet_type = 'control'
					instance = self.redis_demod_manager.demods[instance_uuid]
					system_uuid = self.get_system_from_instance(instance_uuid)

					if instance_uuid not in self.instance_metadata:
                                                self.instance_metadata[instance_uuid] = {'channel_identifier_table': {}, 'patches': {}, 'call_table': {}}

					if system_uuid not in self.system_metadata:
						self.system_metadata[system_uuid] = {'call_table': {}}

					if 'crc' in t and t['crc'] != 0:
						return #Don't bother trying to work with bad data
					if packet_type == 'control':
	                                        if t['name'] == 'IDEN_UP_VU' and t['crc'] == 0:
							with self.lock:
								try:
			                                                self.instance_metadata[instance_uuid]['channel_identifier_table'][t['Identifier']] = {
	        	                                                'BW': t['BW VU'],
	                	                                        'Base Frequency': t['Base Frequency'],
	                        	                                'Channel Spacing': t['Channel Spacing'],
	                                	                        'Transmit Offset': t['Transmit Offset VU'],
	                                        	                'Type': 'FDMA',
	                                                	        'Slots': 1,
		                                                        }
								except:
									pass
						elif t['name'] == 'IDEN_UP' and t['crc'] == 0:
							with self.lock:
								try:
			                                                self.instance_metadata[instance_uuid]['channel_identifier_table'][t['Identifier']] = {
		                                                        'BW': t['BW'],
		                                                        'Base Frequency': t['Base Frequency'],
		                                                        'Channel Spacing': t['Channel Spacing'],
		                                                        'Transmit Offset': t['Transmit Offset'],
		                                                        'Type': 'FDMA',
		                                                        'Slots': 1,
		                                                        }
								except:
									pass
						elif t['name'] == 'IDEN_UP_TDMA' and t['crc'] == 0:
							with self.lock:
								try:
			                                                self.instance_metadata[instance_uuid]['channel_identifier_table'][t['Identifier']] = {
		                                                        'BW': t['BW'],
		                                                        'Base Frequency': t['Base Frequency'],
		                                                        'Channel Spacing': t['Channel Spacing'],
		                                                        'Transmit Offset': t['Transmit Offset TDMA'],
		                                                        'Type': t['Access Type'],
		                                                        'Slots': t['Slots'],
		                                                        }
								except:
									pass
						elif t['name'] == 'GRP_V_CH_GRANT' :
							self.log.debug('GRP_V_CH_GRANT %s %s %s %s' % (instance_uuid, t['Channel'], t['Group Address'], t['Source Address']))
							self.call_user_to_group(instance_uuid, t['Channel'], t['Group Address'], t['Source Address'])
						elif t['name'] == 'MOT_PAT_GRP_VOICE_CHAN_GRANT':
							self.log.debug('MOT_PAT_GRP_VOICE_CHAN_GRANT %s %s %s %s' % (instance_uuid, t['Channel'], t['Super Group'], t['Source Address']))
							self.call_user_to_group(instance_uuid, t['Channel'], t['Super Group'], t['Source Address'])
						elif t['name'] == 'GRP_V_CH_GRANT_UPDT':
							self.log.debug('GRP_V_CH_GRANT_UPDT %s %s %s %s %s' % (instance_uuid, t['Channel 0'], t['Group Address 0'], t['Channel 1'], t['Group Address 1']))
							self.call_continuation(instance_uuid, t['Channel 0'], t['Group Address 0'])
							self.call_continuation(instance_uuid, t['Channel 1'], t['Group Address 1'])
						elif t['name'] == 'MOT_PAT_GRP_VOICE_CHAN_GRANT_UPDT':
							self.log.debug('MOT_PAT_GRP_VOICE_CHAN_GRANT_UPDT %s %s %s %s %s' % (instance_uuid, t['Channel 0'], t['Super Group 0'], t['Channel 1'], t['Super Group 1']))
	                                                self.call_continuation(instance_uuid, t['Channel 0'], t['Super Group 0'])
	                                                self.call_continuation(instance_uuid, t['Channel 1'], t['Super Group 1'])
						elif t['name'] == 'MOT_PAT_GRP_ADD_CMD':
							for group in [t['Group 1'], t['Group 2'], t['Group 3']]:
								pass
								#if(t['Super Group'] in self.patches):
				                                #        self.instance_metadata[instance_uuid]['patches'][t['Super Group']][group] = time()
				                                #else:
				                                #        self.instance_metadata[instance_uuid]['patches'][t['Super Group']] = {group: time()}
						elif t['name'] == 'MOT_PAT_GRP_DEL_CMD':
							#not sure if this is right, but it looks like all 4 groups are the "super" group, so I'll iterate all and teardown any patches in that supergroup
							for group in [t['Super Group'], t['Group 1'], t['Group 2'], t['Group 3']]:
								if(group in self.patches):
									for subgroup in self.patches[group]:
										pass
										#do nothing, not sure the timing works out on this, in example dump there is voice activity 1s before deletion
										#since we take 0.5-3s to timeout a voice call, lets just let the timeout handle patch deletion.
										#self.patches[group][subgroup] = time()-(self.patch_timeout*2) #time immedietly?
					elif packet_type == 'voice':
						try:
							if t['packet']['short'] == 'TLC' and t['packet']['lc']['lcf_long'] == 'Call Termination / Cancellation':
								self.log.debug('closing due to tlc %s %s' % (t['instance_uuid'], t['call_uuid']))
								
								if time.time()-self.instance_metadata[instance_uuid][t['call_uuid']]['time_open'] > 0.2:
									self.close_call(t['instance_uuid'], t['call_uuid'])
							elif t['packet']['lc']['lcf_long'] == 'Group Voice Channel User':
								try:
									channel = self.instance_metadata[t['instance_uuid']]['call_table'][t['call_uuid']]['system_channel_local']
								except:
									channel = -1
								
								if self.instance_metadata[instance_uuid]['call_table'][t['call_uuid']]['system_user_local'] == 0 and t['packet']['lc']['source_id'] != 0:
									self.instance_metadata[instance_uuid]['call_table'][t['call_uuid']]['system_user_local'] = t['packet']['lc']['source_id']

								self.log.debug('call_user_to_group %s %s %s %s' % (instance_uuid, channel, t['packet']['lc']['tgid'], t['packet']['lc']['source_id']))
								if channel != -1:
									self.call_continuation(instance_uuid, channel, t['packet']['lc']['tgid'])
									#self.call_user_to_group(instance_uuid, channel,t['packet']['lc']['tgid'], t['packet']['lc']['source_id'])
							elif t['packet']['lc']['lcf_long'] == 'Group Voice Channel Update':
								self.log.debug('group voice channel update %s %s %s %s' % (t['packet']['lc']['channel_a'], t['packet']['lc']['channel_a_group'], t['packet']['lc']['channel_b'], t['packet']['lc']['channel_b_group']))
								#self.call_user_to_group(instance_uuid, t['packet']['lc']['channel_a'] ,t['packet']['lc']['channel_a_group'], 0)
								#self.call_user_to_group(instance_uuid, t['packet']['lc']['channel_b'] ,t['packet']['lc']['channel_b_group'], 0)
						except KeyError:
							pass

				except Exception as e:
					raise
					self.log.fatal('except: %s' % e)
					self.connection_issue = True

if __name__ == '__main__':
	with open('config.logging.json', 'rt') as f:
	    config = json.load(f)

	logging.config.dictConfig(config)

	main = p25_call_manager()
	while True:
		time.sleep(1)
