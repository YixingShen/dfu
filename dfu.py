import argparse
import logging
import sys
import os
import colorama
import dataclasses
from time import sleep
import timeit
from typing import Any, List, Optional
import usb.core
import usb.util
from usb.backend import libusb1
import _version

logger = logging.getLogger()

# Default USB request timeout
_TIMEOUT_MS = 5000
_DETACH_DELAY_S = 5

# DFU states
_DFU_STATE_APP_IDLE = 0x00
_DFU_STATE_APP_DETACH = 0x01
_DFU_STATE_DFU_IDLE = 0x02
_DFU_STATE_DFU_DNLOAD_SYNC = 0x03
_DFU_STATE_DFU_DNBUSY = 0x04
_DFU_STATE_DFU_DOWNLOAD_IDLE = 0x05
_DFU_STATE_DFU_MANIFEST_SYNC = 0x06
_DFU_STATE_DFU_MANIFEST = 0x07
_DFU_STATE_DFU_MANIFEST_WAIT_RST = 0x08
_DFU_STATE_DFU_UPLOAD_IDLE = 0x09
_DFU_STATE_DFU_ERROR = 0x0A

# DFU status
_DFU_STATUS_OK               = 0x00
_DFU_STATUS_ERR_TARGET       = 0x01
_DFU_STATUS_ERR_FILE         = 0x02
_DFU_STATUS_ERR_WRITE        = 0x03
_DFU_STATUS_ERR_ERASE        = 0x04
_DFU_STATUS_ERR_CHECK_ERASED = 0x05
_DFU_STATUS_ERR_PROG         = 0x06
_DFU_STATUS_ERR_VERIFY       = 0x07
_DFU_STATUS_ERR_ADDRESS      = 0x08
_DFU_STATUS_ERR_NOTDONE      = 0x09
_DFU_STATUS_ERR_FIRMWARE     = 0x0A
_DFU_STATUS_ERR_VENDOR       = 0x0B
_DFU_STATUS_ERR_USBR         = 0x0C
_DFU_STATUS_ERR_POR          = 0x0D
_DFU_STATUS_ERR_UNKNOWN      = 0x0E
_DFU_STATUS_ERR_STALLEDPKT   = 0x0F

# DFU commands
_DFU_CMD_DETACH    = 0
_DFU_CMD_DOWNLOAD  = 1
_DFU_CMD_UPLOAD    = 2
_DFU_CMD_GETSTATUS = 3
_DFU_CMD_CLRSTATUS = 4
_DFU_CMD_GETSTATE  = 5
_DFU_CMD_ABORT     = 6

# DFU Des
_DFU_DESCRIPTOR_LEN  = 9
_DFU_DESC_FUNCTIONAL = 0x21

# mode
CMD_NONE = 0
CMD_VERSION = 1
CMD_LIST = 2
CMD_DETACH = 3
CMD_UPLOAD = 4
CMD_DOWNLOAD = 5
CMD_DOWNLOAD_RANDOM_BIN = 6

# DFU protocol
_DFU_PROTOCOL_NONE = 0x00
_DFU_PROTOCOL_RT  = 0x01
_DFU_PROTOCOL_DFU = 0x02

# DFU bmAttributes
_DFU_CAN_DOWNLOAD    = (1 << 0)
_DFU_CAN_UPLOAD      = (1 << 1)
_DFU_MANIFEST_TOL    = (1 << 2)
_DFU_WILL_DETACH     = (1 << 3)

@dataclasses.dataclass
class dfu_status:
  bStatus: int
  bwPollTimeout: int
  bState: int

@dataclasses.dataclass
class DfuDescriptor:
  bmAttributes: int
  wDetachTimeOut: int
  wTransferSize: int
  bcdDFUVersion: int

class ProgressBar:
  bar_string_fmt = "\rProgress: [{}{}] {:.2%} {}/{}"
  cnt = 0
  
  def __init__(self, total, bar_total=30):
    self.total = total
    self.bar_total = bar_total
  
  def update(self, step=1, value=None):
    total = self.total
    if (value is None):
      self.cnt += step
    else:
      self.cnt = value
    
    bar_cnt = (int((self.cnt/total)*self.bar_total))
    space_cnt = self.bar_total - bar_cnt
    
    progress = self.bar_string_fmt.format(
      "█" * bar_cnt,
      " " * space_cnt,
      self.cnt/total,
      self.cnt,
      total
    )
  
    print(colorama.Style.NORMAL + colorama.Fore.YELLOW + progress, end="    ")
    percent = self.cnt/total

    if percent >= 1:
      print(colorama.Style.RESET_ALL + colorama.Fore.RESET + "\n")

def get_dfu_descriptor(dev: usb.core.Device) -> Optional[DfuDescriptor]:
  for cfg in dev:
    for intf in cfg:
      # pyusb does not seem to automatically parse DFU descriptors
      dfu_desc = intf.extra_descriptors
      if (len(dfu_desc) == _DFU_DESCRIPTOR_LEN and dfu_desc[1] == _DFU_DESC_FUNCTIONAL):
          desc = DfuDescriptor(
            bmAttributes=dfu_desc[2],
            wDetachTimeOut=dfu_desc[4] << 8 | dfu_desc[3],
            wTransferSize=dfu_desc[6] << 8 | dfu_desc[5],
            bcdDFUVersion=dfu_desc[8] << 8 | dfu_desc[7],
          )
          logger.debug("DFU descriptor: %s", desc)
          return desc
  return None

def dfu_get_state(
    dev: usb.core.Device, interface: int, timeout_ms: int = _TIMEOUT_MS
) -> dfu_status:
  bmRequestType = usb.util.build_request_type(
                  usb.util.CTRL_IN,
                  usb.util.CTRL_TYPE_CLASS,
                  usb.util.CTRL_RECIPIENT_INTERFACE
  )
  status = dev.ctrl_transfer(
    bmRequestType=bmRequestType,
    bRequest=_DFU_CMD_GETSTATUS,
    wValue=0,
    wIndex=interface,
    data_or_wLength=6,
    timeout=timeout_ms,
  )
  
  status = dfu_status(
    bStatus=status[0],
    bwPollTimeout=((0xff & status[3]) << 16) |((0xff & status[2]) << 8)  | (0xff & status[1]),
    bState=status[4]
  )
  
  return status

def dfu_clear_status(
  dev: usb.core.Device, interface: int, timeout_ms: int = _TIMEOUT_MS
) -> Any:
  bmRequestType = usb.util.build_request_type(
                  usb.util.CTRL_OUT,
                  usb.util.CTRL_TYPE_CLASS,
                  usb.util.CTRL_RECIPIENT_INTERFACE
  )
  ret = dev.ctrl_transfer(
      bmRequestType=bmRequestType,
      bRequest=_DFU_CMD_CLRSTATUS,
      wValue=0,
      wIndex=interface,
      data_or_wLength=None,
      timeout=timeout_ms,
  )

  return ret

def dfu_abort_status(
  dev: usb.core.Device, interface: int, timeout_ms: int = _TIMEOUT_MS
) -> Any:
  bmRequestType = usb.util.build_request_type(
                  usb.util.CTRL_OUT,
                  usb.util.CTRL_TYPE_CLASS,
                  usb.util.CTRL_RECIPIENT_INTERFACE
  )
  ret = dev.ctrl_transfer(
      bmRequestType=bmRequestType,
      bRequest=_DFU_CMD_ABORT,
      wValue=0,
      wIndex=interface,
      data_or_wLength=None,
      timeout=timeout_ms,
  )
  
  return ret

def dfu_detch(
  dev: usb.core.Device, interface: int, timeout_ms: int = _TIMEOUT_MS
) -> Any:
  bmRequestType = usb.util.build_request_type(
                  usb.util.CTRL_OUT,
                  usb.util.CTRL_TYPE_CLASS,
                  usb.util.CTRL_RECIPIENT_INTERFACE
  )
  ret = dev.ctrl_transfer(
      bmRequestType=bmRequestType,
      bRequest=_DFU_CMD_DETACH,
      wValue=0,
      wIndex=interface,
      data_or_wLength=None,
      timeout=timeout_ms,
  )
  
  return ret

def dfu_download(
  dev: usb.core.Device,
  interface: int,
  transaction: int,
  data: Optional[bytes],
  timeout_ms: int = _TIMEOUT_MS,
) -> None:
  bmRequestType = usb.util.build_request_type(
                  usb.util.CTRL_OUT,
                  usb.util.CTRL_TYPE_CLASS,
                  usb.util.CTRL_RECIPIENT_INTERFACE
  )
  dev.ctrl_transfer(
      bmRequestType=bmRequestType,
      bRequest=_DFU_CMD_DOWNLOAD,
      wValue=transaction,
      wIndex=interface,
      data_or_wLength=data,
      timeout=timeout_ms,
  )
  
  while (True):
    status = dfu_get_state(dev, interface, timeout_ms=timeout_ms)
    #print(status.bState)
  
    if (status.bState == _DFU_STATE_DFU_DOWNLOAD_IDLE or status.bState == _DFU_STATE_DFU_IDLE):
      break
    elif (status.bStatus != _DFU_STATUS_OK or status.bState == _DFU_STATE_DFU_ERROR):
      dfu_clear_status(dev, interface, timeout_ms=timeout_ms)
      raise RuntimeError(f"status is not OK: {status.bState} {status.bStatus}")
    else :
      sleep(status.bwPollTimeout/1000)

def dfu_upload(
  dev: usb.core.Device,
  interface: int,
  transaction: int,
  xfersize: int,
  timeout_ms: int = _TIMEOUT_MS,
) -> bytes:
  bmRequestType = usb.util.build_request_type(
                  usb.util.CTRL_IN,
                  usb.util.CTRL_TYPE_CLASS,
                  usb.util.CTRL_RECIPIENT_INTERFACE
  )
  data = dev.ctrl_transfer(
      bmRequestType=bmRequestType,
      bRequest=_DFU_CMD_UPLOAD,
      wValue=transaction,
      wIndex=interface,
      data_or_wLength=xfersize,
      timeout=timeout_ms,
  )

  while (True):
    status = dfu_get_state(dev, interface, timeout_ms=timeout_ms)
    #print(status.bState)
  
    if (status.bState == _DFU_STATE_DFU_UPLOAD_IDLE or status.bState == _DFU_STATE_DFU_IDLE):
      break
    elif (status.bStatus != _DFU_STATUS_OK or status.bState == _DFU_STATE_DFU_ERROR):
      dfu_clear_status(dev, interface, timeout_ms=timeout_ms)
      raise RuntimeError(f"status is not OK: {status.bState} {status.bStatus}")
    else :
      sleep(status.bwPollTimeout/1000)

  return data

def dfu_claim_interface(dev: usb.core.Device, interface: int, alt: int) -> None:
  print(f"Claiming USB DFU interface: {interface}")
  usb.util.claim_interface(dev, interface)

def dfu_release_interface(dev: usb.core.Device) -> None:
  print(f"Releasing USB DFU interface")
  usb.util.dispose_resources(dev)

def _get_dfu_devices(
  vid: Optional[int] = None, pid: Optional[int] = None
) -> List[usb.core.Device]:
  class FilterDFU:  # pylint: disable=too-few-public-methods
    """Identify DFU devices"""
    def __call__(self, device: usb.core.Device) -> bool:
      if vid is None or vid == device.idVendor:
        if pid is None or pid == device.idProduct:
          for cfg in device:
            for intf in cfg:
              if (intf.bInterfaceClass == 0xFE and intf.bInterfaceSubClass == 1):
                return True
      return False
  
  localpath_libusb1_win32 = os.path.dirname(sys.argv[0]) + "\libusb-1.0.dll"
  if sys.platform == 'win32' and os.path.exists(localpath_libusb1_win32):
    back = libusb1.get_backend(find_library=lambda x:localpath_libusb1_win32)
    return list(usb.core.find(find_all=True, backend=back, custom_match=FilterDFU()))
  else :
    return list(usb.core.find(find_all=True, custom_match=FilterDFU()))

def _dfu_download(
  dev: usb.core.Device, interface: int, data: bytes, xfersize: int
) -> None:
  transaction = 0
  bytes_downloaded = 0
  _totol = len(data)
  progressbar = ProgressBar(total=_totol, bar_total=30)

  try:
    while bytes_downloaded < len(data):
        chunk_size = min(xfersize, len(data) - bytes_downloaded)
        chunk = data[bytes_downloaded : bytes_downloaded + chunk_size]
  
        logger.debug(
            "Downloading %d bytes (total: %d bytes)",
            chunk_size,
            bytes_downloaded,
        )
  
        dfu_download(dev, interface, transaction, chunk)
        progressbar.update(value=bytes_downloaded)
        transaction += 1
        bytes_downloaded += chunk_size

    # send one zero sized download request to signalize end
    dfu_download(dev, interface, transaction, None)
    progressbar.update(value=progressbar.total)
  except usb.core.USBError as err:
    logger.warning("Ignoring USB error when exiting DFU: %s", err)

def _dfu_upload(
  dev: usb.core.Device, interface: int, transferSize: int
) -> bytes:
  transaction = 0
  bytes_uploaded = 0
  _totol = int(args.upload_size)
  progressbar = ProgressBar(total=_totol, bar_total=30)
  data = bytes()

  try:
    while True:
      rdata = dfu_upload(dev, interface, transaction, transferSize)
      if bytes_uploaded < progressbar.total:
        progressbar.update(value=bytes_uploaded)
      else :
        progressbar.update(value=progressbar.total-1)
  
      data += rdata
      bytes_uploaded += len(rdata)
      transaction += 1
  
      if len(rdata) < transferSize :
        break
  
    progressbar = ProgressBar(total=bytes_uploaded)
    progressbar.update(value=bytes_uploaded)
  except usb.core.USBError as err:
    logger.warning("Ignoring USB error when exiting DFU: %s", err)
  
  return data

def list_devices(vid: Optional[int] = None, pid: Optional[int] = None) -> None:
  devicelist = _get_dfu_devices(vid=vid, pid=pid)
  
  if not devicelist:
    print("No DFU devices found")
  else :
    for device in devicelist:
        print("DFU devices: Bus {} Device {:03d}: ID {:04x}:{:04x}".format(device.bus, device.address, device.idVendor, device.idProduct))

def download(
  dev: usb.core.Device,
  filename: str,
  interface: int = 0,
  transferSize: int = 0,
) -> int:
  print(f"Downloading binary file: {filename}")

  if not os.path.exists(filename) :
    print(f"not exists: {filename}")
    return 1
  
  if not os.access(filename, os.R_OK) :
    print(f"not readable: {filename}")
    return 1
  
  fin = open(filename, "rb")
  
  try:
    status = dfu_get_state(dev, interface)
    if (status.bState == _DFU_STATE_APP_IDLE or status.bState == _DFU_STATE_APP_DETACH):
      print(f"Device still run in Run-Time Mode, status.bState = {status.bState}")
      return 1
      
    status = dfu_get_state(dev, interface)
    if (status.bStatus != _DFU_STATUS_OK or status.bState == _DFU_STATE_DFU_ERROR):
      print("error clear status")
      print(f"send DFU_CLRSTATUS")
      ret = dfu_clear_status(dev, interface)
      if ret < 0:
        return 1
    
    status = dfu_get_state(dev, interface)
    if (status.bState == _DFU_STATE_DFU_DOWNLOAD_IDLE or status.bState == _DFU_STATE_DFU_UPLOAD_IDLE):
      print("aborting previous incomplete transfer")
      print(f"send DFU_ABORT")
      ret = dfu_abort_status(dev, interface)
      if ret < 0:
        print(f"can't send DFU_ABORT")
        return 1
    
      status = dfu_get_state(dev, interface)
      if (status.bState == _DFU_STATE_DFU_DOWNLOAD_IDLE or status.bState == _DFU_STATE_DFU_UPLOAD_IDLE):
        print(f"abort is not OK")
        return 1
      else :
        print(f"abort is OK")
    
    data = fin.read()
    _dfu_download(dev, interface, data, transferSize)
  finally:
    fin.close()
  
  return 0

def upload(
  dev: usb.core.Device,
  filename: str,
  interface: int = 0,
  transferSize: int = 0,
) -> int:
  print(f"Uploading binary file: {filename}")
  fout = open(filename, "wb")
  
  if not os.access(filename, os.W_OK) :
     print(f"not writable: {filename}")
     return 1
  
  try:
    status = dfu_get_state(dev, interface)
    if (status.bState == _DFU_STATE_APP_IDLE or status.bState == _DFU_STATE_APP_DETACH):
      print(f"Device still run in Run-Time Mode, status.bState = {status.bState}")
      return 1

    status = dfu_get_state(dev, interface)
    if (status.bStatus != _DFU_STATUS_OK or status.bState == _DFU_STATE_DFU_ERROR):
      print("error clear status")
      print(f"send DFU_CLRSTATUS")
      ret = dfu_clear_status(dev, interface)
      if ret < 0:
        return 1

    status = dfu_get_state(dev, interface)
    if (status.bState == _DFU_STATE_DFU_DOWNLOAD_IDLE or status.bState == _DFU_STATE_DFU_UPLOAD_IDLE):
      print("aborting previous incomplete transfer")
      print(f"send DFU_ABORT")
      ret = dfu_abort_status(dev, interface)
      if ret < 0:
        print(f"can't send DFU_ABORT")
        return 1
    
      status = dfu_get_state(dev, interface)
      if (status.bState == _DFU_STATE_DFU_DOWNLOAD_IDLE or status.bState == _DFU_STATE_DFU_UPLOAD_IDLE):
        print(f"abort is not OK")
        return 1
      else :
        print(f"abort is OK")
    
    data = _dfu_upload(dev, interface, transferSize)
    if len(data) > 0:
      fout.write(data)
  finally:
    fout.close()
  
  return 0

def detch(
  dev: usb.core.Device,
  interface: int = 0,
) -> int:
  try:
    ret = dfu_detch(dev, interface)
  
    if ret < 0:
      print(f"can't send DFU_DETACH")
      return 1
    else :
      print(f"send DFU_DETACH")
      print(f"delay {args.detach_delay} sec")
      sleep(args.detach_delay)
  finally:
    return 0

def get_dfu_device(
  vid: Optional[int] = None, pid: Optional[int] = None
):
  transfer_size = args.transfer_size
  interface = 0
  dfu_mode = 0
  altsetting = args.match_iface_alt_index
  dev = None
  devices = _get_dfu_devices(vid=vid, pid=pid)
  
  if not devices:
    print("No DFU devices found")
    return dev, dfu_mode, interface, altsetting, transfer_size

  if len(devices) > 1:
    print(f"Too many DFU devices ({len(devices)}). List devices for "
           "more info and specify vid:pid to filter.")
    return dev, dfu_mode, interface, altsetting, transfer_size

  dev = devices[0]

  if (dev.get_active_configuration() == None):
    try:
      dev.set_configuration()
    except usb.core.USBError as e:
      raise ValueError("Could not set configuration: %s" % str(e))
  
  dfu_desc = get_dfu_descriptor(dev)

  if dfu_desc is None:
    raise ValueError("No DFU Functional descriptor, is this a valid DFU device?")

  bitWillDetach = False
  
  if (dfu_desc.bmAttributes & _DFU_WILL_DETACH):
      bitWillDetach = True

  if args.verbose:
    print(f"DFU Functional descriptor:")
    print(f" bcdDFUVersion = 0x{dfu_desc.bcdDFUVersion:04X}")
    print(f" wDetachTimeOut = 0x{dfu_desc.wDetachTimeOut}")
    print(f" wTransferSize = 0x{dfu_desc.wTransferSize}")
    print(f" bmAttributes = 0x{dfu_desc.bmAttributes}")
  
    if (dfu_desc.bmAttributes & _DFU_CAN_DOWNLOAD):
      print(f"  bitCanDnload = 1")
    else :
      print(f"  bitCanDnload = 0")

    if (dfu_desc.bmAttributes & _DFU_CAN_UPLOAD):
      print(f"  bitCanUpload = 1")
    else :
      print(f"  bitCanUpload = 0")

    if (dfu_desc.bmAttributes & _DFU_MANIFEST_TOL):
      print(f"  bitManifestationTolerant = 1")
    else :
      print(f"  bitManifestationTolerant = 0")

    if (dfu_desc.bmAttributes & _DFU_WILL_DETACH):
      print(f"  bitWillDetach = 1")
    else :
      print(f"  bitWillDetach = 0")

  #if dfu_desc.bcdDFUVersion != 0x0101 :
  #  raise ValueError("bcdDFUVersion != 0x0101")

  if (transfer_size <= 0) :
    transfer_size = dfu_desc.wTransferSize

  for cfg in dev:
    for intf in cfg:
      if (intf.bInterfaceClass == 0xFE and intf.bInterfaceSubClass == 1):
        interface = intf.bInterfaceNumber
        if (intf.bInterfaceProtocol == _DFU_PROTOCOL_DFU):
          dfu_mode = _DFU_PROTOCOL_DFU
  
        break

  if (args.interface >= 0):
    interface = args.interface
  
  altsetting = args.match_iface_alt_index
  return dev, dfu_mode, interface, altsetting, transfer_size

def main() -> int:
  command = CMD_NONE

  if args.device:
    vidpid = args.device.split(":")
    if len(vidpid) !=1 and len(vidpid) != 2:
        logger.error("Invalid device argument: %s", args.device)
        return 1

    if len(vidpid) == 1:
      vid = vidpid[0]
      pid = ''
    if len(vidpid) == 2:
      vid, pid = vidpid
    
    if vid != '':
      vid = int(vid, 16)
    else:
      vid = None
    
    if pid != '':
      pid = int(pid, 16)
    else:
      pid = None
  else:
    vid, pid = None, None

  if args.list:
    command = CMD_LIST

  if args.download_file:
    command = CMD_DOWNLOAD

  if args.random_bin_file_size != 0:
    command = CMD_DOWNLOAD_RANDOM_BIN
    
  if args.upload_file:
    command = CMD_UPLOAD

  if args.detach:
    command = CMD_DETACH

  if args.version:
    command = CMD_VERSION

  print(f"dfu.py version {_version.__version__}")

  if command == CMD_VERSION:
    return 0

  if command == CMD_NONE:
    print("No command specified")
    return 0

  if args.verbose:
    print(f"command = {command}")

  dfu_device = None

  try:
    error = 0

    if command == CMD_LIST:
      list_devices(vid=vid, pid=pid)
      return error

    dfu_device, dfu_mode, interface, altsetting, transfer_size = get_dfu_device(vid=vid, pid=pid)

    if dfu_device == None:
      return 1

    if args.verbose:
      print(f"get_dfu_device:")
      print(f" vid:pid = {dfu_device.idVendor:04x}:{dfu_device.idProduct:04x}")
      print(f" dfu_mode = {dfu_mode}")
      print(f" selected interface = {interface}")
      print(f" selected altsetting = {altsetting}")
      print(f" selected transfer size = {transfer_size}")

    if command == CMD_DETACH:
      dfu_claim_interface(dfu_device, interface, altsetting)
      dfu_device.set_interface_altsetting(interface, altsetting)
      error = detch(dfu_device, interface)
      dfu_release_interface(dfu_device)
      return error

    if dfu_mode != _DFU_PROTOCOL_DFU:
      print(f"Device is running in run-time mode")
      dfu_claim_interface(dfu_device, interface, altsetting)
      dfu_device.set_interface_altsetting(interface, altsetting)

      status = dfu_get_state(dfu_device, interface)
      sleep(status.bwPollTimeout/1000)

      if (status.bStatus != _DFU_STATUS_OK or status.bState == _DFU_STATE_DFU_ERROR):
          print("send DFU_CLRSTATUS")
          if dfu_clear_status(dfu_device, interface) < 0:
            dfu_release_interface(dfu_device)
            return 1

      if (status.bState == _DFU_STATE_APP_IDLE or status.bState == _DFU_STATE_APP_DETACH):
        print("Device is really in run-time mode, send DFU detach request")
        error = detch(dfu_device, interface)
        if error != 0:
          return 1

        dfu_release_interface(dfu_device)
        dfu_device, dfu_mode, interface, altsetting, transfer_size = get_dfu_device(vid=vid, pid=pid)

        if dfu_device == None:
          return 1

        if args.verbose:
          print(f"get_dfu_device:")
          print(f" vid:pid = {dfu_device.idVendor:04x}:{dfu_device.idProduct:04x}")
          print(f" dfu_mode = {dfu_mode}")
          print(f" selected interface = {interface}")
          print(f" selected altsetting = {altsetting}")
          print(f" selected transfer size = {transfer_size}")

        if dfu_mode != _DFU_PROTOCOL_DFU:
          print(f"Failed! device is still in run-time mode")
          return 1

    print(f"Device is really in dfu mode")
    dfu_claim_interface(dfu_device, interface, altsetting)
    dfu_device.set_interface_altsetting(interface, altsetting)

    if command == CMD_DOWNLOAD or command == CMD_DOWNLOAD_RANDOM_BIN:
      if command == CMD_DOWNLOAD:
        download_file = args.download_file

      if command == CMD_DOWNLOAD_RANDOM_BIN:
        fileSize = args.random_bin_file_size
        download_file = "_tmp_random.bin"
        fout = open(download_file, "wb")
        fout.write(os.urandom(fileSize))
        fout.close()
      
        if not os.access(download_file, os.W_OK) :
           print(f"not writable: {download_file}")
           return 1

      start = timeit.default_timer()
      error = download(
        dev=dfu_device,
        filename=download_file,
        interface=interface,
        transferSize=transfer_size
      )
      stop = timeit.default_timer()
      print(f"The elapsed time = {stop - start:f} sec")

    if command == CMD_UPLOAD:
      start = timeit.default_timer()
      error = upload(
        dev=dfu_device,
        filename=args.upload_file,
        interface=interface,
        transferSize=transfer_size
      )
      stop = timeit.default_timer()
      print(f"The elapsed time = {stop - start:f} sec")

    if args.final_detach and error == 0:
      detch(dfu_device, interface)
      print(f"delay {args.detach_delay} sec")
      sleep(args.detach_delay)

      # bitWillDetach: device will perform a bus 
      # detach-attach sequence when it receives a DFU_DETACH request. 
      # The host must not issue a USB Reset. (bitWillDetach)
      # 0 = no; 1 = yes
      if bitWillDetach is False :
        print("issue usb reset")
        dfu_device.reset()

    dfu_release_interface(dfu_device)
    return error
  except (
    RuntimeError,
    ValueError,
    FileNotFoundError,
    IsADirectoryError,
    usb.core.USBError,
  ) as err:
    if dfu_device != None:
      dfu_release_interface(dfu_device)
    if command == CMD_DOWNLOAD:
      logger.error("DFU download failed: %s", repr(err))
    elif command == CMD_UPLOAD:
      logger.error("DFU upload failed: %s", repr(err))
    elif command == CMD_DETACH:
      logger.error("DFU detach failed: %s", repr(err))
    elif command == CMD_LIST:
      logger.error("DFU list failed: %s", repr(err))
    else :
      logger.error("failed: %s", repr(err))

    return 1

if __name__ == '__main__':
  parser = argparse.ArgumentParser(description="Device firmware update (DFU) USB programmer")
  parser.add_argument(
    "-V",
    "--version",
    dest="version",
    help="Print the version number",
    action="store_true",
    default=False,
  )
  parser.add_argument(
    "-l",
    "--list",
    dest="list",
    help="List currently attached DFU capable devices",
    action="store_true",
    default=False,
  )
  parser.add_argument(
    "-D",
    "--download",
    dest="download_file",
    help="Download firmware from <file> to device",
    required=False,
  )
  parser.add_argument(
    "-U",
    "--upload",
    dest="upload_file",
    help="Read firmware from device into <file>",
    required=False,
  )
  parser.add_argument(
    "-d",
    "--device",
    dest="device",
    help="Specify DFU device in hex as <vid> or <vid>:<pid>",
    required=False,
  )
  parser.add_argument(
    "-i",
    "--intf",
    dest="interface",
    help="Specify the DFU Interface number. default is -1 \"auto detect DFU interface from USB descriptor\"",
    required=False,
    type=lambda x: int(x,0),
    default=-1,
  )
  parser.add_argument(
    "-a",
    "--alt",
    dest="match_iface_alt_index",
    help="Specify the Altsetting of the DFU Interface by number. default is 0",
    required=False,
    type=lambda x: int(x,0),
    default=0,
  )
  parser.add_argument(
    "-t",
    "--transfer-size",
    dest="transfer_size",
    help="Specify the number of bytes per USB Transfer",
    required=False,
    type=lambda x: int(x,0),
    default=0,
  )
  parser.add_argument(
    "-Z",
    "--upload-size",
    dest="upload_size",
    help="Specify the expected upload size, in bytes",
    required=False,
    type=lambda x: int(x,0),
    default=1024*1024*32, #32M Bytes
  )
  parser.add_argument(
    "-e",
    "--detach",
    dest="detach",
    help="Detach currently attached DFU capable devices",
    action="store_true",
    default=False,
  )
  parser.add_argument(
    "-E",
    "--detach-delay",
    dest="detach_delay",
    help="seconds Time to wait before reopening a device after detach",
    required=False,
    type=lambda x: int(x,0),
    default=_DETACH_DELAY_S,
  )
  parser.add_argument(
    "-R",
    "--reset",
    dest="final_detach",
    help="detach and issue USB Reset (if bitWillDetach = 0) signalling",
    action="store_true",
    default=False,
  )
  parser.add_argument(
    "-v",
    "--verbose",
    dest="verbose",
    help="Print verbose debug statements",
    action="store_true",
    default=False,
  )
  parser.add_argument(
    "-G",
    "--generate-random-bin-file-download",
    dest="random_bin_file_size",
    help="generate a random binary file \"_tmp_random.bin\" and download it to device",
    required=False,
    type=lambda x: int(x,0),
    default=4096,
  )

  args = parser.parse_args()
  logging.basicConfig(level=logging.INFO)

  if args.verbose:
    print(f'version = {args.version}')
    print(f'verbose = {args.verbose}')
    print(f'list = {args.list}')
    print(f'download_file = {args.download_file}')
    print(f'upload_file = {args.upload_file}')
    print(f'device = {args.device}')
    print(f'interface = {args.interface}')
    print(f'match_iface_alt_index = {args.match_iface_alt_index}')
    print(f'transfer_size = {args.transfer_size}')
    print(f'upload_size = {args.upload_size}')
    print(f'detach = {args.detach}')
    print(f'detach_delay = {args.detach_delay}')
    print(f'final_detach = {args.final_detach}')
    print(f'random_bin_file_size = {args.random_bin_file_size}')

  sys.exit(main())
