import argparse
import logging
import sys
from time import sleep
import dataclasses
import os
import usb.core
import usb.util
from usb.backend import libusb1
import colorama
from typing import Any, List, Optional

logger = logging.getLogger()
logging.basicConfig(level=logging.INFO)

# Default USB request timeout
_TIMEOUT_MS = 5000
detach_delay = 5

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
_DFU_CMD_DETACH = 0
_DFU_CMD_DOWNLOAD = 1
_DFU_CMD_UPLOAD = 2
_DFU_CMD_GETSTATUS = 3
_DFU_CMD_CLRSTATUS = 4
_DFU_CMD_GETSTATE = 5
_DFU_CMD_ABORT = 6

# DFU Des
_DFU_DESCRIPTOR_LEN = 9
_DFU_DESC_FUNCTIONAL = 0x21

# mode
MODE_NONE = 0
MODE_VERSION = 1
MODE_LIST = 2
MODE_DETACH = 3
MODE_UPLOAD = 4
MODE_DOWNLOAD = 5

# DFU protocol
_DFU_PROTOCOL_NONE = 0x00
_DFU_PROTOCOL_RT  = 0x01
_DFU_PROTOCOL_DFU = 0x02

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

        print(colorama.Style.NORMAL + colorama.Fore.YELLOW + progress, end="")
        percent = self.cnt/total

        if percent >= 1:
            print(colorama.Style.RESET_ALL + colorama.Fore.RESET + "\n")

def get_dfu_descriptor(dev: usb.core.Device) -> Optional[DfuDescriptor]:
    for cfg in dev:
        for intf in cfg:
            # pyusb does not seem to automatically parse DFU descriptors
            dfu_desc = intf.extra_descriptors
            if (
                len(dfu_desc) == _DFU_DESCRIPTOR_LEN
                and dfu_desc[1] == _DFU_DESC_FUNCTIONAL
            ):
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
      elif (status.bState == _DFU_STATE_DFU_ERROR):
        dfu_clear_status(dev, interface, timeout_ms=timeout_ms)
        raise RuntimeError(f"state is not OK: {status.bState} {status.bStatus}")
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
      elif (status.bState == _DFU_STATE_DFU_ERROR):
        dfu_clear_status(dev, interface, timeout_ms=timeout_ms)
        raise RuntimeError(f"state is not OK: {status.bState} {status.bStatus}")
      else :
        sleep(status.bwPollTimeout/1000)

    return data

def dfu_claim_interface(dev: usb.core.Device, interface: int, alt: int) -> None:
    logger.info("Claiming USB DFU interface %d", interface)
    usb.util.claim_interface(dev, interface)

def dfu_release_interface(dev: usb.core.Device) -> None:
    logger.info("Releasing USB DFU interface")
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
                            if (
                                intf.bInterfaceClass == 0xFE
                                and intf.bInterfaceSubClass == 1
                            ):
                                return True
            return False

    back = libusb1.get_backend(find_library=lambda x: r"./libusb-1.0.dll")
    return list(usb.core.find(find_all=True, backend=back, custom_match=FilterDFU()))

def _get_dfu_mode_devices(
    vid: Optional[int] = None, pid: Optional[int] = None
) -> List[usb.core.Device]:
    class FilterDFU:  # pylint: disable=too-few-public-methods
        """Identify devices which are in DFU mode."""
        def __call__(self, device: usb.core.Device) -> bool:
            if vid is None or vid == device.idVendor:
                if pid is None or pid == device.idProduct:
                    for cfg in device:
                        for intf in cfg:
                            if (
                                intf.bInterfaceClass == 0xFE
                                and intf.bInterfaceSubClass == 1
                                and intf.bInterfaceProtocol == _DFU_PROTOCOL_DFU
                            ):
                                return True
            return False

    back = libusb1.get_backend(find_library=lambda x: r"./libusb-1.0.dll")
    return list(usb.core.find(find_all=True, backend=back, custom_match=FilterDFU()))

def _dfu_download(
    dev: usb.core.Device, interface: int, data: bytes, xfersize: int
) -> None:
  transaction = 0
  bytes_downloaded = 0
  progressbar = ProgressBar(len(data))

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
  
        transaction += 1
        bytes_downloaded += chunk_size
        progressbar.update(value=bytes_downloaded)

    # send one zero sized download request to signalize end
    dfu_download(dev, interface, transaction, None)
    #progressbar.update(value=progressbar.total)
  except usb.core.USBError as err:
    logger.warning("Ignoring USB error when exiting DFU: %s", err)

def _dfu_upload(
    dev: usb.core.Device, interface: int, transferSize: int
) -> bytes:
    transaction = 0
    data = bytes()
    progressbar = ProgressBar(100)

    while True:
      try:
        rdata = dfu_upload(dev, interface, transaction, transferSize)
        data += rdata

        if len(rdata) < transferSize :
          break

        transaction += 1
        progressbar.update(value=(transaction%100))
      except usb.core.USBError as err:
        logger.warning("Ignoring USB error when exiting DFU: %s", err)

    progressbar.update(value=progressbar.total)
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
    logger.info("Downloading binary file: %s", filename)

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
    logger.info("Uploading binary file: %s", filename)
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
      global detach_delay
      ret = dfu_detch(dev, interface)

      if ret < 0:
        print(f"can't send DFU_DETACH")
        return 1
      else :
        print(f"send DFU_DETACH")
        print(f"delay {detach_delay} sec")
        sleep(detach_delay)
    finally:
      return 0
  
def main() -> int:
  mode = MODE_NONE
  global dfu_mode_devices
  dfu_mode_devices = None

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
  
  # List DFU devices
  if args.list:
    mode = MODE_LIST
    list_devices(vid=vid, pid=pid)
    return 0

  if args.download_file:
     mode = MODE_DOWNLOAD

  if args.upload_file:
     mode = MODE_UPLOAD

  if args.detach:
     mode = MODE_DETACH

  try:
    if mode == MODE_DETACH :
      dfu_devices = _get_dfu_devices(vid=vid, pid=pid)
      if not dfu_devices:
          raise RuntimeError("No DFU devices found")
  
      if len(dfu_devices) > 1:
          raise RuntimeError(
              f"Too many devices ({len(dfu_devices)}). List devices for "
              "more info and specify vid:pid to filter."
          )
  
      dev = dfu_devices[0]

    elif mode == MODE_DOWNLOAD or mode == MODE_UPLOAD:
      dfu_mode_devices = _get_dfu_mode_devices(vid=vid, pid=pid)
  
      if not dfu_mode_devices:
          raise RuntimeError("No devices found in DFU mode")
  
      if len(dfu_mode_devices) > 1:
          raise RuntimeError(
              f"Too many devices in DFU mode ({len(dfu_mode_devices)}). List devices for "
              "more info and specify vid:pid to filter."
          )

      dev = dfu_mode_devices[0]

    if mode == MODE_DETACH or mode == MODE_DOWNLOAD or mode == MODE_UPLOAD:
      if (dev.get_active_configuration() == None):
          try:
              dev.set_configuration()
          except usb.core.USBError as e:
              raise ValueError("Could not set configuration: %s" % str(e))
  
      dfu_desc = get_dfu_descriptor(dev)
  
      if dfu_desc is None:
        raise ValueError("No DFU descriptor, is this a valid DFU device?")
  
      if dfu_desc.bcdDFUVersion != 0x0101 :
        raise ValueError("bcdDFUVersion != 0x0101")
  
      transfer_size = args.transfer_size

      if (transfer_size <= 0) :
        transfer_size = dfu_desc.wTransferSize

      interface = 0

      for cfg in dev:
        for intf in cfg:
          if (intf.bInterfaceClass == 0xFE and intf.bInterfaceSubClass == 1):
            interface = intf.bInterfaceNumber
            break

      if (args.interface >= 0):
        interface = args.interface

      altsetting = args.match_iface_alt_index

      if mode == MODE_DETACH :
        dfu_claim_interface(dev, interface, altsetting)
        dev.set_interface_altsetting(interface, altsetting)

        error = detch(
          dev=dev,
          interface=interface
        )

        dfu_release_interface(dev)
        return error

      if mode == MODE_DOWNLOAD:
        dfu_claim_interface(dev, interface, altsetting)
        dev.set_interface_altsetting(interface, altsetting)

        error = download(
          dev=dev,
          filename=args.download_file,
          interface=interface,
          transferSize=transfer_size
        )

        dfu_release_interface(dev)
        return error

      if mode == MODE_UPLOAD:
        dfu_claim_interface(dev, interface, altsetting)
        dev.set_interface_altsetting(interface, altsetting)

        error = upload(
          dev=dev,
          filename=args.upload_file,
          interface=interface,
          transferSize=transfer_size
        )

        dfu_release_interface(dev)
        return error

    print("No command specified")
    return 0
  except (
    RuntimeError,
    ValueError,
    FileNotFoundError,
    IsADirectoryError,
    usb.core.USBError,
  ) as err:
    if mode == MODE_DOWNLOAD:
      logger.error("DFU download failed: %s", repr(err))
    elif mode == MODE_UPLOAD:
      logger.error("DFU upload failed: %s", repr(err))
    else :
      logger.error("failed: %s", repr(err))

    return 1

if __name__ == '__main__':
  parser = argparse.ArgumentParser(description="DFU 1.1 utility")
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
    help="Read firmware from device into <file",
    required=False,
  )
  parser.add_argument(
    "-d",
    "--device",
    dest="device",
    help="Specify DFU device in hex as <vid>:<pid>",
    required=False,
  )
  parser.add_argument(
    "-i",
    "--intf",
    dest="interface",
    help="Specify the DFU Interface number",
    required=False,
    type=lambda x: int(x,0),
    default=-1,
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
    "-a",
    "--alt",
    dest="match_iface_alt_index",
    help="Specify the Altsetting of the DFU Interface by number",
    required=False,
    type=lambda x: int(x,0),
    default=0,
  )
  parser.add_argument(
    "-e",
    "--detach",
    dest="detach",
    help="Detach currently attached DFU capable devices",
    action="store_true",
    default=False,
  )

  args = parser.parse_args()
  sys.exit(main())
