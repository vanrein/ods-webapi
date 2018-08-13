# backrabbitmq.py -- Adding and removing zones via Rabbit MQ messaging.
#
# This is where you cause managing and unmanaging of zones.
# The implementation is through sending commands to a queue:
#  - ADDKEY zone.tld
#  - DELKEY zone.tld
# These will be sent to a local queue named keyops.  The reason
# the queue is local is that ods-rpc does not distribute the
# operations; PKCS #11 replication cares for replication.  Also
# note that, beyond key management, there is no work for zone
# addition and removal, because that is done via rsync zone file
# updates (including additions and removals of zone files).
#
# The rabbitdnssec library from RabbitDNSSEC is used here.  This is
# done to be able to share configuration variables and thus to avoid
# the need for double configurations and the management errors that
# would come from it.  Note that the RPC directory is still set in
# two places; it does not only impact this backend, and so it would
# burden non-RabbitMQ generic code with the RabbitDNSSEC library.
#
# The routines return 0 on success, nonzero on failure.
#
# From: Rick van Rein <rick@openfortress.nl>


import os
import sys
import socket
import time
import ssl

import threading

import pika


import rabbitdnssec
from rabbitdnssec import log_debug, log_info, log_notice, log_warning, log_error, log_critical


cfg = rabbitdnssec.my_config ('ods-rpc')
# When used with RabbitDNSSEC, this should always be 'key_ops':
routing_key = cfg.get ('routing_key', 'key_ops')
cluster_key = cfg.get ('cluster_key', '')  # '' means no clustering
username = cfg ['username']
rpcdir = cfg.get ('rpc_dir', '/var/opendnssec/rpc')
exchange_name = rabbitdnssec.my_exchange ()
cluster_queue = rabbitdnssec.my_queue (cluster_key)


creds   = rabbitdnssec.my_credentials (ovr_username=username)
cnxparm = rabbitdnssec.my_connectionparameters (creds)
cnx = None
chan = None
try:
        cnx = pika.BlockingConnection (cnxparm)
        chan = cnx.channel ()
	log_info ('Ready to start sending to RabbitMQ')
except pika.exceptions.AMQPChannelError, e:
	log_debug ('AMQP Channel Error:', e)
        sys.exit (1)
except pika.exceptions.AMQPError, e:
	log_error ('AMQP Error:', e)
        sys.exit (1)

# Confirm delivery with the return value from chan.basic_publish()
#
chan.confirm_delivery ()

#
# API routine: add a zone to keyed management, return zero on success
#
def manage_zone (zone):
	cmd = 'ADDKEY ' + zone
	try:
		log_debug ('Sending to exchange', exchange_name, 'routing_key', routing_key, 'body', cmd)
		ok = chan.basic_publish (exchange=exchange_name,
                                        routing_key=routing_key,
                                        body=cmd,
					mandatory=True)
		log_debug ('Send success is', ok)
		retval = 0 if ok else 1
	except pika.exceptions.AMQPChannelError, e:
		log_error ('AMQP Channel Error:', e)
		retval = 1
	except pika.exceptions.AMQPError, e:
		log_error ('AMQP Error:', e)
		retval = 1
	except Exception, e:
		log_error ('Exception during AMQP send:', e, 'for zone', zone, 'during ADDKEY')
		retval = 1
	return retval

#
# API routine: remove a zone from keyed management, return zero on success
#
def unmanage_zone (zone):
	cmd = 'DELKEY ' + zone
	try:
		log_debug ('Sending to exchange', exchange_name, 'routing_key', routing_key, 'body', cmd)
		ok = chan.basic_publish (exchange=exchange_name,
                                        routing_key=routing_key,
                                        body=cmd,
					mandatory=True)
		log_debug ('Send success is', ok)
		retval = 0 if ok else 1
	except pika.exceptions.AMQPChannelError, e:
		log_error ('AMQP Channel Error:', e)
		retval = 1
	except pika.exceptions.AMQPError, e:
		log_error ('AMQP Error:', e)
		retval = 1
	except Exception, e:
		log_error ('Exception during AMQP send:', e, 'for zone', zone, 'during DELKEY')
		retval = 1
	return retval

#
# API routine: notify other cluster nodes, if any, about a new flag state
#
def cluster_update (zone_flag, value):
	if cluster_key == '':
		# No action towards a cluster
		return True
	now = int (time.time ())
	if value is not None:
		cmd = str (now) + ' SET '   + zone_flag + ' ' + value
	else:
		cmd = str (now) + ' CLEAR ' + zone_flag + ' '
	try:
		log_debug ('Sending to exchange', exchange_name, 'cluster_key', cluster_key, 'body', cmd)
		ok = chan.basic_publish (exchange=exchange_name,
                                        routing_key=cluster_key,
                                        body=cmd,
					mandatory=True)
		log_debug ('Send success is', ok)
		retval = ok
	except pika.exceptions.AMQPChannelError, e:
		log_error ('AMQP Channel Error:', e)
		retval = False
	except pika.exceptions.AMQPError, e:
		log_error ('AMQP Error:', e)
		retval = False
	except Exception, e:
		log_error ('Exception during AMQP send:', e, 'for zone', zone, 'during ADDKEY')
		retval = False
	return retval


# Process updates from other cluster nodes
#
cluster_start = int (time.time ())
#
def process_cluster_msg (chan, msg, props, body):
	log_debug ('Processing cluster message', body)
	try:
		(cmd,time_str,zone_flag,value) = body.split (' ',3)
		time_int = int (time_str)
		flag_path = rpcdir + '/' + os.sep + zone_flag
		if cmd == 'CLEAR':
			if os.stat (rpcdir).st_mtime < time_str:
				os.unlink (flag_path)
				log_debug ('Removed RPC flag', zone_flag)
		elif cmd == 'SET':
			#TODO# Check timestamp
			if os.stat (flag_path).st_mtime < time_str:
				open (flag_path, 'w').write (value)
				log_debug ('Set RPC flag', zone_flag, 'to', value)
		else:
			raise Exception ('Unknown command')
	except Exception,e:
		log_error ('Failed to process cluster message', body, 'due to', str (e))
#
class ClusterRecipient (threading.Thread):
	#
	def __init__ (self, chan):
		self.chan = chan
		threading.Thread.__init__ (self)
	#
	def run (self):
		self.chan.basic_consume (process_cluster_msg, queue=cluster_queue)
#
if cluster_key != '':
	while True:
		(msg,props,body) = chan.basic_get (queue=cluster_queue)
		if msg is None:
			break
		process_cluster_msg (chan, msg, props, body)
	cluster_recipient = ClusterRecipient (chan)
	cluster_recipient.start ()



# Not here: The client will want the connection open
#
# if chan is not None:
# 	chan = None
# if cnx is not None:
# 	cnx.close ()
# cnx = None
# 

