#!/usr/bin/env python
# -*- coding: utf-8 -*-
# generated by wxGlade 0.6.3 on Wed Apr 11 17:48:58 2012

from ConfigParser import ConfigParser, NoOptionError
from client import Client
from datetime import datetime
from isp import Isp
from measure import Measure
from optparse import OptionParser
from os import path
from profile import Profile
from server import Server
from sys import platform
from sysmonitor import checkset, RES_CPU, RES_RAM, RES_WIFI, RES_TRAFFIC, RES_HOSTS
from task import Task
from tester import Tester
from threading import Thread
from time import sleep
from timeNtp import timestampNtp
from urlparse import urlparse
from xmlutils import xml2task
import hashlib
import httputils
import logging
import paths
import ping
import pythoncom
import re
import sysmonitor
import wmi
import wx
from prospect import Prospect

__version__ = '2.2'

# Tempo di attesa tra una misura e la successiva in caso di misura fallita
TIME_LAG = 5
DOWN = 'down'
UP = 'up'
# Soglia per il rapporto tra traffico 'spurio' e traffico totale
TH_TRAFFIC = 0.1
TH_TRAFFIC_INV = 0.9
# Soglia per numero di pacchetti persi
TH_PACKETDROP = 0.05

TOTAL_STEPS = 12

logger = logging.getLogger()

def sleeper():
    sleep(.001)
    return 1 # don't forget this otherwise the timeout will be removed

class OptionParser(OptionParser):

  def check_required(self, opt):
    option = self.get_option(opt)
    if getattr(self.values, option.dest) is None:
      self.error('%s option not supplied' % option)

class _Checker(Thread):

  def __init__(self, gui, checkable_set = set([RES_CPU, RES_RAM, RES_WIFI, RES_HOSTS])):
    Thread.__init__(self)
    self._gui = gui
    self._checkable_set = checkable_set

  def run(self):
    #wx.CallAfter(self._gui._update_messages, "Profilazione dello stato del sistema di misurazione")
    profiled_set = checkset(self._checkable_set)

    for resource in checkable_set:
      wx.CallAfter(self._gui.set_resource_info, resource, profiled_set[resource])

class _Tester(Thread):

  def __init__(self, gui):
    Thread.__init__(self)
    paths.check_paths()
    self._outbox = paths.OUTBOX
    self._prospect = Prospect()

    self._gui = gui
    self._step = 0

    (options, args, md5conf) = parse()

    self._client = getclient(options)
    self._scheduler = options.scheduler
    self._tasktimeout = options.tasktimeout
    self._testtimeout = options.testtimeout
    self._httptimeout = options.httptimeout
    self._md5conf = md5conf

    self._running = True

  def join(self, timeout = None):
    logger.debug("Richiesta di close")
    #wx.CallAfter(self._gui._update_messages, "Attendere la chiusura del programma...")
    self._running = False

  def _test_gating(self, test, testtype):
    '''
    Funzione per l'analisi del contabit ed eventuale gating dei risultati del test
    '''
    stats = test.counter_stats
    logger.debug('Valori di test: %s' % stats)
    continue_testing = True

    logger.debug('Analisi della percentuale dei pacchetti persi')
    packet_drop = stats.packet_drop
    packet_tot = stats.packet_tot_all
    if (packet_tot > 0):
      packet_ratio = float(packet_drop) / float(packet_tot)
      logger.debug('Percentuale di pacchetti persi: %.2f%%' % (packet_ratio * 100))
      if (packet_tot > 0 and packet_ratio > TH_PACKETDROP):
        info = 'Eccessiva presenza di traffico di rete, impossibile analizzare i dati di test'
        wx.CallAfter(self._gui.set_resource_info, RES_TRAFFIC, {'status': False, 'info': info, 'value': None})
        return continue_testing

    else:
      info = 'Errore durante la misura, impossibile analizzare i dati di test'
      wx.CallAfter(self._gui.set_resource_info, RES_TRAFFIC, {'status': False, 'info': info, 'value': None})
      return continue_testing

    if (testtype == DOWN):
      byte_nem = stats.payload_down_nem_net
      byte_all = byte_nem + stats.byte_down_oth_net
      packet_nem_inv = stats.packet_up_nem_net
      packet_all_inv = packet_nem_inv + stats.packet_up_oth_net
    else:
      byte_nem = stats.payload_up_nem_net
      byte_all = byte_nem + stats.byte_up_oth_net
      packet_nem_inv = stats.packet_down_nem_net
      packet_all_inv = packet_nem_inv + stats.packet_down_oth_net

    logger.debug('Analisi dei rapporti di traffico')
    if byte_all > 0 and packet_all_inv > 0:
      traffic_ratio = float(byte_all - byte_nem) / float(byte_all)
      packet_ratio_inv = float(packet_all_inv - packet_nem_inv) / float(packet_all_inv)
      value = round(traffic_ratio * 100)
      logger.info('kbyte_nem: %.1f; kbyte_all %.1f; packet_nem_inv: %d; packet_all_inv: %d' % (byte_nem / 1024.0, byte_all / 1024.0, packet_nem_inv, packet_all_inv))
      logger.debug('Percentuale di traffico spurio: %.2f%%/%.2f%%' % (traffic_ratio * 100, packet_ratio_inv * 100))
      if traffic_ratio < 0:
        wx.CallAfter(self._gui._update_messages, 'Errore durante la verifica del traffico di misura: impossibile salvare i dati.', 'red')
        return continue_testing
      elif traffic_ratio < TH_TRAFFIC and packet_ratio_inv < TH_TRAFFIC_INV:
        # Dato da salvare sulla misura
        test.bytes = byte_all
        info = 'Traffico internet non legato alla misura: percentuali %d%%/%d%%.' % (value, round(packet_ratio_inv * 100))
        wx.CallAfter(self._gui.set_resource_info, RES_TRAFFIC, {'status': True, 'info': info, 'value': value})
        return True
      else:
        info = 'Eccessiva presenza di traffico internet non legato alla misura: percentuali %d%%/%d%%.' % (value, round(packet_ratio_inv * 100))
        wx.CallAfter(self._gui.set_resource_info, RES_TRAFFIC, {'status': False, 'info': info, 'value': value})
        return continue_testing
    else:
      info = 'Errore durante la misura, impossibile analizzare i dati di test'
      wx.CallAfter(self._gui.set_resource_info, RES_TRAFFIC, {'status': False, 'info': info, 'value': value})
      return continue_testing

    return True

  def _get_bandwith(self, test):

    if test.value > 0:
      return int(round(test.bytes * 8 / test.value))
    else:
      raise Exception("Errore durante la valutazione del test")

  def _update_gauge(self):
    self._step += 1
    wx.CallAfter(self._gui.update_gauge, self._step)

  def _get_server(self):

    servers = set([Server('NAMEX', '193.104.137.133', 'NAP di Roma'), Server('MIX', '193.104.137.4', 'NAP di Milano')])

    maxRTT = 8000
    RTT = {maxRTT:None}
    
    for repeat in range(3):
      for server in servers:
        try:
          delay = ping.do_one("%s" % server.ip, 1)
          RTT[delay] = server
        except Exception as e:
          logger.debug('Errore durante il ping dell\'host %s: %s' % (server.ip, e))
          pass

    for key in RTT:
      logger.debug('RTT vector: %s - %s[ms]' % (RTT[key],key))
    
    if min(RTT)<=maxRTT:
      return RTT[min(RTT)]
    
    wx.CallAfter(self._gui._update_messages, "Non è stato possibile contattare il server di misura, la misurazione non può essere effettuata. Contattare l'helpdesk del progetto Misurainternet per avere informazioni sulla risoluzione del problema.", 'red')
    return None

  def _check_usb(self, device_id):
    result = False

    if re.search('5B5B1B000BAD&0', device_id):
      result = True

    return result

  def _check_usb_devices(self):

    result = False

    pythoncom.CoInitialize()
    info = wmi.WMI()
    for usb in info.Win32_DiskDrive(InterfaceType = 'USB'):
      logger.debug("Trovato device USB: %s" % usb.PNPDeviceID)
      if (self._check_usb(usb.PNPDeviceID)):
        result = True
        break
    pythoncom.CoUninitialize()

    return True
    return result

  def run(self):

    if (not self._check_usb_devices()):
      logger.debug('Verifica della presenza della pennetta USB fallita')
      wx.CallAfter(self._gui._update_messages, "Attenzione: per eseguire le misure occorre disporre della penna USB fornita per l'installazione dei Ne.Me.Sys.", 'red')

    else:

      logger.debug('Preparazione alla misurazione...')
      wx.CallAfter(self._gui._update_messages, "Preparazione alla misurazione...")
      self._update_gauge()

      # Profilazione
      self._check_system(set([RES_HOSTS, RES_WIFI]))

      # TODO Il server deve essere indicato dal backend che è a conoscenza dell'occupazione della banda!

      # TODO Rimuovere dopo aver sistemato il backend
      task = None
      server = self._get_server()
      if server != None:
        wx.CallAfter(self._gui._update_messages, "Identificato il server di misura: %s" % server.name)

        # Scaricamento del task dallo scheduler
        task = self._download_task(server)
        self._update_gauge()
        task = Task(0, '2010-01-01 10:01:00', server, '/download/1000.rnd', 'upload/1000.rnd', 3, 3, 10, 4, 4, 0, True)

      if task != None:

        try:
          start = datetime.fromtimestamp(timestampNtp())

          ip = sysmonitor.getIp()
          t = Tester(if_ip = ip, host = task.server, timeout = self._testtimeout,
                     username = self._client.username, password = self._client.password)

          id = start.strftime('%y%m%d%H%M')
          m = Measure(id, task.server, self._client, __version__, start.isoformat())


          # Testa gli ftp down
          # ------------------------
          i = 1;
          while (i <= task.download and self._running):

            self._check_system(set([RES_CPU, RES_RAM]))

            # Esecuzione del test
            test = t.testftpdown(task.ftpdownpath)
            bandwidth = self._get_bandwith(test)
            task.update_ftpdownpath(bandwidth)

            self._update_gauge()
            wx.CallAfter(self._gui._update_messages, "Fine del test %d di %d di FTP download." % (i, task.download), 'blue')

            if i > 1:
              # Analisi da contabit
              if (self._test_gating(test, DOWN)):
                i = i + 1
            else:
              i = i + 1

          # Salvataggio dell'ultima misura
          m.savetest(test)
          wx.CallAfter(self._gui._update_down, self._get_bandwith(test))

          # Testa gli ftp up
          # ------------------------
          i = 1;
          while (i <= task.upload and self._running):

            self._check_system(set([RES_CPU, RES_RAM]))

            # Esecuzione del test
            test = t.testftpup(self._client.profile.upload * task.multiplier * 1000 / 8, task.ftpuppath)
            bandwidth = self._get_bandwith(test)
            self._client.profile.upload = max(bandwidth, 40000 / 8 * 10)

            self._update_gauge()
            wx.CallAfter(self._gui._update_messages, "Fine del test %d di %d di FTP upload." % (i, task.download), 'blue')

            if i > 1:
              # Analisi da contabit
              if (self._test_gating(test, UP)):
                i = i + 1
            else:
              i = i + 1

          # Salvataggio dell'ultima misura
          m.savetest(test)
          wx.CallAfter(self._gui._update_up, bandwidth)

          # Ping
          i = 1
          self._check_system(set([RES_CPU, RES_RAM]))

          while (i <= task.ping and self._running):

            test = t.testping()
            self._update_gauge()
            wx.CallAfter(self._gui._update_messages, "Fine del test %d di %d di ping." % (i, task.ping), 'blue')

            if ((i + 2) % task.nicmp == 0):
              sleep(task.delay)
              self._check_system(set([RES_CPU, RES_RAM]))

            i = i + 1

          # Salvataggio dell'ultima misura
          m.savetest(test)
          wx.CallAfter(self._gui._update_ping, test.value)

          self._save_measure(m)
          self._prospect.save_measure(m)

        except Exception as e:
          logger.warning('Misura sospesa per eccezione %s' % e)
          wx.CallAfter(self._gui._update_messages, 'Misura sospesa per errore: %s. Aspetta qualche secondo prima di effettuare una nuova misura.' % e)

        # Stop
        sleep(TIME_LAG)

    wx.CallAfter(self._gui.stop)
    self.join()

  def _save_measure(self, measure):
    # Salva il file con le misure
    sec = datetime.fromtimestamp(timestampNtp()).strftime('%S')
    f = open('%s/measure_%s%s.xml' % (self._outbox, measure.id, sec), 'w')
    f.write(str(measure))

    # Aggiungi la data di fine in fondo al file
    f.write('\n<!-- [finished] %s -->' % datetime.fromtimestamp(timestampNtp()).isoformat())
    f.close()

  def _check_system(self, checkable_set = set([RES_CPU, RES_RAM, RES_WIFI, RES_HOSTS])):
    #wx.CallAfter(self._gui._update_messages, "Profilazione dello stato del sistema di misurazione")
    profiled_set = checkset(checkable_set)

    for resource in checkable_set:
      wx.CallAfter(self._gui.set_resource_info, resource, profiled_set[resource])

  # Scarica il prossimo task dallo scheduler
  def _download_task(self, server):

    url = urlparse(self._scheduler)
    connection = httputils.getverifiedconnection(url = url, certificate = None, timeout = self._httptimeout)

    try:
      connection.request('GET', '%s?clientid=%s&version=%s&confid=%s&server=%s' % (url.path, self._client.id, __version__, self._md5conf, server.ip))
      data = connection.getresponse().read()
    except Exception as e:
      logger.error('Impossibile scaricare lo scheduling. Errore: %s.' % e)
      return None

    return xml2task(data)

class Frame(wx.Frame):
    def __init__(self, *args, **kwds):
        self._tester = None

        # begin wxGlade: Frame.__init__
        wx.Frame.__init__(self, *args, **kwds)

        self.sizer_3_staticbox = wx.StaticBox(self, -1, "Messaggi")
        self.bitmap_button_play = wx.BitmapButton(self, -1, wx.Bitmap(path.join(paths.ICONS, u"play.png"), wx.BITMAP_TYPE_ANY))
        self.bitmap_button_check = wx.BitmapButton(self, -1, wx.Bitmap(path.join(paths.ICONS, u"check.png"), wx.BITMAP_TYPE_ANY))
        self.bitmap_5 = wx.StaticBitmap(self, -1, wx.Bitmap(path.join(paths.ICONS, u"logo_nemesys.png"), wx.BITMAP_TYPE_ANY))
        self.label_5 = wx.StaticText(self, -1, "", style = wx.ALIGN_CENTRE)
        self.label_6 = wx.StaticText(self, -1, "Speedtest", style = wx.ALIGN_CENTRE)
        self.bitmap_cpu = wx.StaticBitmap(self, -1, wx.Bitmap(path.join(paths.ICONS, u"%s_gray.png" % RES_CPU.lower()), wx.BITMAP_TYPE_ANY))
        self.bitmap_ram = wx.StaticBitmap(self, -1, wx.Bitmap(path.join(paths.ICONS, u"%s_gray.png" % RES_RAM.lower()), wx.BITMAP_TYPE_ANY))
        self.bitmap_wifi = wx.StaticBitmap(self, -1, wx.Bitmap(path.join(paths.ICONS, u"%s_gray.png" % RES_WIFI.lower()), wx.BITMAP_TYPE_ANY))
        self.bitmap_hosts = wx.StaticBitmap(self, -1, wx.Bitmap(path.join(paths.ICONS, u"%s_gray.png" % RES_HOSTS.lower()), wx.BITMAP_TYPE_ANY))
        self.bitmap_traffic = wx.StaticBitmap(self, -1, wx.Bitmap(path.join(paths.ICONS, u"%s_gray.png" % RES_TRAFFIC.lower()), wx.BITMAP_TYPE_ANY))
        self.label_cpu = wx.StaticText(self, -1, "%s\n- - - -" % RES_CPU, style = wx.ALIGN_CENTRE)
        self.label_ram = wx.StaticText(self, -1, "%s\n- - - -" % RES_RAM, style = wx.ALIGN_CENTRE)
        self.label_wifi = wx.StaticText(self, -1, "%s\n- - - -" % RES_WIFI, style = wx.ALIGN_CENTRE)
        self.label_hosts = wx.StaticText(self, -1, "%s\n- - - -" % RES_HOSTS, style = wx.ALIGN_CENTRE)
        self.label_traffic = wx.StaticText(self, -1, "%s\n- - - -" % RES_TRAFFIC, style = wx.ALIGN_CENTRE)
        self.gauge_1 = wx.Gauge(self, -1, TOTAL_STEPS, style = wx.GA_HORIZONTAL | wx.GA_SMOOTH)
        self.label_r_1 = wx.StaticText(self, -1, "Ping", style = wx.ALIGN_CENTRE)
        self.label_r_2 = wx.StaticText(self, -1, "Download", style = wx.ALIGN_CENTRE)
        self.label_r_3 = wx.StaticText(self, -1, "Upload", style = wx.ALIGN_CENTRE)
        self.label_rr_ping = wx.StaticText(self, -1, "- - - -", style = wx.ALIGN_CENTRE)
        self.label_rr_down = wx.StaticText(self, -1, "- - - -", style = wx.ALIGN_CENTRE)
        self.label_rr_up = wx.StaticText(self, -1, "- - - -", style = wx.ALIGN_CENTRE)
        self.messages_area = wx.TextCtrl(self, -1, "", style = wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH | wx.TE_RICH2 | wx.TE_WORDWRAP)
        self.grid_sizer_1 = wx.GridSizer(2, 5, 0, 0)
        self.grid_sizer_2 = wx.GridSizer(2, 3, 0, 0)

        self.__set_properties()
        self.__do_layout()

        #self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_BUTTON, self._play, self.bitmap_button_play)
        self.Bind(wx.EVT_BUTTON, self._check, self.bitmap_button_check)
        # end wxGlade

    def _on_close_event(self, event):

      logger.debug("Richiesta di close")
      #if (self._tester and self._tester != None):
      #  self._tester.join()
      self.Destroy()

    def __set_properties(self):
        # begin wxGlade: Frame.__set_properties
        self.SetTitle("Ne.Me.Sys Speedtest")
        self.SetSize((720, 420))
        self.bitmap_button_play.SetMinSize((120, 120))
        self.bitmap_button_check.SetMinSize((40, 120))
        self.bitmap_5.SetMinSize((95, 65))
        #self.label_5.SetFont(wx.Font(18, wx.ROMAN, wx.NORMAL, wx.NORMAL, 0, ""))
        self.label_6.SetFont(wx.Font(14, wx.ROMAN, wx.ITALIC, wx.NORMAL, 0, ""))
        self.bitmap_cpu.SetMinSize((60, 60))
        self.bitmap_ram.SetMinSize((60, 60))
        self.bitmap_wifi.SetMinSize((60, 60))
        self.bitmap_hosts.SetMinSize((60, 60))
        self.bitmap_traffic.SetMinSize((60, 60))
        self.gauge_1.SetMinSize((700, 26))
        self.label_rr_ping.SetFont(wx.Font(12, wx.SWISS, wx.NORMAL, wx.BOLD, 0, ""))
        self.label_rr_down.SetFont(wx.Font(12, wx.SWISS, wx.NORMAL, wx.BOLD, 0, ""))
        self.label_rr_up.SetFont(wx.Font(12, wx.SWISS, wx.NORMAL, wx.BOLD, 0, ""))

        self.messages_area.SetMinSize((700, 121))
        self.messages_area.SetFont(wx.Font(11, wx.SWISS, wx.NORMAL, wx.NORMAL, 0, ""))
        self.grid_sizer_2.SetMinSize((700, 60))

        #self.SetBackgroundColour(wx.SystemSettings_GetColour(wx.SYS_COLOUR_WINDOW))
        self.SetBackgroundColour(wx.Colour(242, 241, 240))

        # end wxGlade

    def __do_layout(self):
        # begin wxGlade: Frame.__do_layout   
        self.grid_sizer_1.Add(self.bitmap_cpu, 0, wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 12)
        self.grid_sizer_1.Add(self.bitmap_ram, 0, wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 12)
        self.grid_sizer_1.Add(self.bitmap_wifi, 0, wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 12)
        self.grid_sizer_1.Add(self.bitmap_hosts, 0, wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 12)
        self.grid_sizer_1.Add(self.bitmap_traffic, 0, wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 12)
        self.grid_sizer_1.Add(self.label_cpu, 0, wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 2)
        self.grid_sizer_1.Add(self.label_ram, 0, wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 2)
        self.grid_sizer_1.Add(self.label_wifi, 0, wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 2)
        self.grid_sizer_1.Add(self.label_hosts, 0, wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 2)
        self.grid_sizer_1.Add(self.label_traffic, 0, wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 2)

        self.grid_sizer_2.Add(self.label_r_1, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 0)
        self.grid_sizer_2.Add(self.label_r_2, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 0)
        self.grid_sizer_2.Add(self.label_r_3, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 0)
        self.grid_sizer_2.Add(self.label_rr_ping, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_TOP, 2)
        self.grid_sizer_2.Add(self.label_rr_down, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_TOP, 2)
        self.grid_sizer_2.Add(self.label_rr_up, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_TOP, 2)

        sizer_1 = wx.BoxSizer(wx.VERTICAL)
        sizer_2 = wx.BoxSizer(wx.HORIZONTAL)
        sizer_4 = wx.BoxSizer(wx.VERTICAL)
        sizer_6 = wx.StaticBoxSizer(self.sizer_3_staticbox, wx.VERTICAL)

        sizer_4.Add(self.bitmap_5, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 2)
        #sizer_4.Add(self.label_5, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 2)
        sizer_4.Add(self.label_6, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 2)

        sizer_2.Add(self.bitmap_button_play, 0, wx.LEFT | wx.ALIGN_RIGHT | wx.ALIGN_TOP, 4)
        sizer_2.Add(self.bitmap_button_check, 0, wx.LEFT | wx.ALIGN_RIGHT | wx.ALIGN_TOP, 4)
        sizer_2.Add(self.grid_sizer_1, 0, wx.RIGHT | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 10)
        sizer_2.Add(sizer_4, 0, wx.RIGHT | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 4)

        sizer_6.Add(self.messages_area, 0, wx.ALL | wx.EXPAND | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 0)

        sizer_1.Add(sizer_2, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 6)
        sizer_1.Add(self.gauge_1, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 0)
        sizer_1.Add(sizer_6, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 6)
        sizer_1.Add(self.grid_sizer_2, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_CENTER_VERTICAL, 2)

        self.SetSizer(sizer_1)
        self.Layout()
        # end wxGlade

        #self._check(None)

    def _check(self, event):
      self.bitmap_button_check.Disable()
      self._check_system()
      self.bitmap_button_check.Enable()

    # TODO Spostare il check in un thread separato
    def _check_system(self, checkable_set = set([RES_CPU, RES_RAM, RES_WIFI, RES_HOSTS])):

      #wx.CallAfter(self._gui._update_messages, "Profilazione dello stato del sistema di misurazione")
      profiled_set = checkset(checkable_set)

      for resource in checkable_set:
        self.set_resource_info(resource, profiled_set[resource])

    def _update_down(self, downwidth):
      self.label_rr_down.SetLabel("%d kbps" % downwidth)
      self.Layout()

    def _update_up(self, upwidth):
      self.label_rr_up.SetLabel("%d kbps" % upwidth)
      self.Layout()

    def _update_ping(self, rtt):
      self.label_rr_ping.SetLabel("%d ms" % rtt)
      self.Layout()

    def _reset_info(self):

      checkable_set = set([RES_CPU, RES_RAM, RES_WIFI, RES_HOSTS, RES_TRAFFIC])

      for resource in checkable_set:
        self.set_resource_info(resource, {'status': None, 'info': None, 'value': None})

      self.label_rr_down.SetLabel("- - - -")
      self.label_rr_up.SetLabel("- - - -")
      self.label_rr_ping.SetLabel("- - - -")

      self.update_gauge(0)
      self.Layout()

    def update_gauge(self, value):
      # logger.debug("Gauge value %d" % value)
      self.gauge_1.SetValue(value)

    def _play(self, event):

      self._reset_info()
      self._tester = _Tester(self)
      self._tester.start()

      #self.bitmap_button_play.SetBitmapLabel(wx.Bitmap(path.join(paths.ICONS, u"stop.png")))
      self.bitmap_button_play.Disable()
      self.bitmap_button_check.Disable()

    def stop(self):

      self.update_gauge(0)

      #self.bitmap_button_play.SetBitmapLabel(wx.Bitmap(path.join(paths.ICONS, u"play.png")))
      self.bitmap_button_play.Enable()
      self.bitmap_button_check.Enable()

      self._update_messages("Sistema pronto per una nuova misura")

    def set_resource_info(self, resource, info):
      res_bitmap = None
      res_label = None

      if info['status'] == None:
        color = 'gray'
      elif info['status'] == True:
        color = 'green'
      else:
        color = 'red'

      if resource == RES_CPU:
        res_bitmap = self.bitmap_cpu
        res_label = self.label_cpu
      elif resource == RES_RAM:
        res_bitmap = self.bitmap_ram
        res_label = self.label_ram
      elif resource == RES_WIFI:
        res_bitmap = self.bitmap_wifi
        res_label = self.label_wifi
      elif resource == RES_HOSTS:
        res_bitmap = self.bitmap_hosts
        res_label = self.label_hosts
      elif resource == RES_TRAFFIC:
        res_bitmap = self.bitmap_traffic
        res_label = self.label_traffic

      if res_bitmap != None:
        res_bitmap.SetBitmap(wx.Bitmap(path.join(paths.ICONS, u"%s_%s.png" % (resource.lower(), color))))

      if info['value'] != None:
        if resource == RES_CPU or resource == RES_RAM or resource == RES_TRAFFIC:
            res_label.SetLabel("%s\n%.1f%%" % (resource, float(info['value'])))
        else:
          res_label.SetLabel("%s\n%s" % (resource, info['value']))
      else:
        res_label.SetLabel("%s\n- - - -" % resource)

      if info['status'] == False:
        self._update_messages("%s: %s" % (resource, info['info']), color)

      self.Layout()

    def _update_messages(self, message, color = 'black'):

      logger.info('Messagio all\'utente: "%s"' % message)
      date = '\n%s' % getdate().strftime('%c')
      self.messages_area.AppendText("%s %s" % (date, message))
      end = self.messages_area.GetLastPosition() - len(message)
      start = end - len(date)
      self.messages_area.SetStyle(start, end, wx.TextAttr(color))

def getdate():
  return datetime.fromtimestamp(timestampNtp())

def parse():
  '''
  Parsing dei parametri da linea di comando
  '''

  config = ConfigParser()

  if (path.exists(paths.CONF_MAIN)):
    config.read(paths.CONF_MAIN)
    logger.info('Caricata configurazione da %s' % paths.CONF_MAIN)

  parser = OptionParser(version = __version__, description = '')

  # Task options
  # --------------------------------------------------------------------------
  section = 'task'
  if (not config.has_section(section)):
    config.add_section(section)

  option = 'tasktimeout'
  value = '3600'
  try:
    value = config.getint(section, option)
  except (ValueError, NoOptionError):
    config.set(section, option, value)
  parser.add_option('--task-timeout', dest = option, type = 'int', default = value,
                    help = 'global timeout (in seconds) for each task [%s]' % value)

  option = 'testtimeout'
  value = '60'
  try:
    value = config.getint(section, option)
  except (ValueError, NoOptionError):
    config.set(section, option, value)
  parser.add_option('--test-timeout', dest = option, type = 'float', default = value,
                    help = 'timeout (in seconds as float number) for each test in a task [%s]' % value)

  option = 'scheduler'
  value = 'https://finaluser.agcom244.fub.it/Scheduler'
  try:
    value = config.get(section, option)
  except (ValueError, NoOptionError):
    config.set(section, option, value)
  parser.add_option('-s', '--scheduler', dest = option, default = value,
                    help = 'complete url for schedule download [%s]' % value)

  option = 'httptimeout'
  value = '60'
  try:
    value = config.getint(section, option)
  except (ValueError, NoOptionError):
    config.set(section, option, value)
  parser.add_option('--http-timeout', dest = option, type = 'int', default = value,
                    help = 'timeout (in seconds) for http operations [%s]' % value)

  # Client options
  # --------------------------------------------------------------------------
  section = 'client'
  if (not config.has_section(section)):
    config.add_section(section)

  option = 'clientid'
  value = None
  try:
    value = config.get(section, option)
  except (ValueError, NoOptionError):
    pass
  parser.add_option('-c', '--clientid', dest = option, default = value,
                    help = 'client identification string [%s]' % value)

  option = 'username'
  value = 'anonymous'
  try:
    value = config.get(section, option)
  except (ValueError, NoOptionError):
    config.set(section, option, value)
  parser.add_option('--username', dest = option, default = value,
                    help = 'username for FTP login [%s]' % value)

  option = 'password'
  value = '@anonymous'
  try:
    value = config.get(section, option)
  except (ValueError, NoOptionError):
    config.set(section, option, value)
  parser.add_option('--password', dest = option, default = value,
                    help = 'password for FTP login [%s]' % value)

  # Profile options
  # --------------------------------------------------------------------------
  section = 'profile'
  if (not config.has_section(section)):
    config.add_section(section)

  option = 'bandwidthup'
  value = 64
  try:
    value = config.getint(section, option)
  except (ValueError, NoOptionError):
    pass
  parser.add_option('--up', dest = option, default = value, type = 'int',
                    help = 'upload bandwidth [%s]' % value)

  option = 'bandwidthdown'
  value = 1000
  try:
    value = config.getint(section, option)
  except (ValueError, NoOptionError):
    pass
  parser.add_option('--down', dest = option, default = value, type = 'int',
                    help = 'download bandwidth [%s]' % value)

  with open(paths.CONF_MAIN, 'w') as file:
    config.write(file)

  (options, args) = parser.parse_args()

  # Verifica che le opzioni obbligatorie siano presenti
  # --------------------------------------------------------------------------

  try:

    parser.check_required('--clientid')
    config.set('client', 'clientid', options.clientid)

    parser.check_required('--up')
    config.set('profile', 'bandwidthup', options.bandwidthup)

    parser.check_required('--down')
    config.set('profile', 'bandwidthdown', options.bandwidthdown)

  finally:
    with open(paths.CONF_MAIN, 'w') as file:
      config.write(file)

  with open(paths.CONF_MAIN, 'r') as file:
    md5 = hashlib.md5(file.read()).hexdigest()

  return (options, args, md5)

def getclient(options):

  profile = Profile(id = None, upload = options.bandwidthup,
                    download = options.bandwidthdown)
  isp = Isp('fub001')
  return Client(id = options.clientid, profile = profile, isp = isp,
                geocode = None, username = options.username,
                password = options.password)

if __name__ == "__main__":

  logger.info('Starting Nemesys v.%s' % __version__)

  app = wx.PySimpleApp(0)
  if platform == 'win32':
    wx.CallLater(200, sleeper)
  wx.InitAllImageHandlers()
  frame_1 = Frame(None, -1, "", style = wx.DEFAULT_FRAME_STYLE & ~(wx.RESIZE_BORDER | wx.RESIZE_BOX))
  app.SetTopWindow(frame_1)
  frame_1.Show()
  app.MainLoop()
