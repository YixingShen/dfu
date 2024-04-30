@echo off
python dfu.py -d cafe -a 0 -t 4096 -U fw_upload.bin
pause