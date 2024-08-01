import argparse
import sys
import os

def main() -> int:
  try:
    if args.file_name and args.file_size:
      fileSize = args.file_size
      fileName = args.file_name

      if fileSize <= 0:
        print(f"Failed! file size {args.file_size}")
        return 1

      fout = open(fileName, "wb")
      fout.write(os.urandom(fileSize))
      fout.close()
      
      if not os.access(fileName, os.W_OK) :
         print(f"generate '{fileName}' failed!")
         return 1
      else :
         print(f"generate file '{fileName}' size {fileSize}")

    return 0
  except (RuntimeError,ValueError,FileNotFoundError,IsADirectoryError) as err:
    print("generate failed: %s", repr(err))
    return 1

if __name__ == '__main__':
  parser = argparse.ArgumentParser(description="generate random bin file")
  parser.add_argument(
    "-f",
    "--file",
    dest="file_name",
    help="Generated binary file name",
    required=True,
  )
  parser.add_argument(
    "-s",
    "--size",
    dest="file_size",
    help="Specify file size",
    required=True,
    type=lambda x: int(x,0),
  )

  args = parser.parse_args()

  sys.exit(main())
