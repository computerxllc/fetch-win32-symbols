#
# This script will read a CSV of modules from Socorro, and try to retrieve
# missing symbols from Microsoft's symbol server. It honors a blacklist
# (blacklist.txt) of symbols that are known to be from our applications,
# and it maintains its own list of symbols that the MS symbol server
# doesn't have (skiplist.txt).
#
# The script must have installed alongside it:
# * msdia80.dll (from the DIA SDK, installed with Visual C++ 8)
# * dbghelp.dll (from WinDBG)
# * symsrv.dll  (also from WinDBG)
# * symsrv.yes  (a zero-byte file that indicates that you've accepted
#                the Microsoft symbol server EULA)
# * config.py   (create this from the template in config.py.in)
#
# The script also depends on having write access to the directory it is
# installed in, to write the skiplist text file.
#
# Finally, you must have 'zip' (Info-Zip), 'scp', and 'ssh' available in %PATH%.

from __future__ import with_statement
import config
import sys
import os
import time, datetime
import subprocess
import StringIO
import gzip
import shutil
import ctypes
import logging
from collections import defaultdict
from tempfile import mkdtemp
from urllib import urlopen

# Just hardcoded here
MICROSOFT_SYMBOL_SERVER = "http://msdl.microsoft.com/download/symbols"

thisdir = os.path.dirname(__file__)

def write_skiplist():
  try:
    with open(os.path.join(thisdir, 'skiplist.txt'), 'w') as sf:
      for (debug_id,debug_file) in skiplist.iteritems():
          sf.write("%s %s\n" % (debug_id, debug_file))
  except IOError:
    log.exception("Error writing skiplist.txt")

verbose = False
if len(sys.argv) > 1 and sys.argv[1] == "-v":
  verbose = True
  sys.argv.pop(1)

log = logging.getLogger()
log.setLevel(logging.DEBUG)
formatter = logging.Formatter(fmt="%(asctime)-15s %(message)s",
                              datefmt="%Y-%m-%d %H:%M:%S")
filelog = logging.FileHandler(filename=os.path.join(thisdir,
                                                    "symsrv-fetch.log"))
filelog.setLevel(logging.INFO)
filelog.setFormatter(formatter)
log.addHandler(filelog)

if verbose:
  handler = logging.StreamHandler()
  handler.setLevel(logging.DEBUG)
  handler.setFormatter(formatter)
  log.addHandler(handler)
  verboselog = logging.FileHandler(filename=os.path.join(thisdir,
                                                      "verbose.log"))
  log.addHandler(verboselog)

log.info("Started")

# Symbols that we know belong to us, so don't ask Microsoft for them.
blacklist=set()
try:
  bf = file(os.path.join(thisdir, 'blacklist.txt'), 'r')
  for line in bf:
      blacklist.add(line.strip().lower())
  bf.close()
except IOError:
  pass
log.debug("Blacklist contains %d items" % len(blacklist))

# Symbols that we've asked for in the past unsuccessfully
skiplist={}
skipcount = 0
try:
  sf = file(os.path.join(thisdir, 'skiplist.txt'), 'r')
  for line in sf:
      line = line.strip()
      if line == '':
          continue
      s = line.split(None, 1)
      if len(s) != 2:
        continue
      (debug_id, debug_file) = s
      skiplist[debug_id] = debug_file.lower()
      skipcount += 1
  sf.close()
except IOError:
  pass
log.debug("Skiplist contains %d items" % skipcount)

modules = defaultdict(set)
if len(sys.argv) > 1:
  url = sys.argv[1]
else:
  date = (datetime.date.today() - datetime.timedelta(1)).strftime("%Y%m%d")
  url = config.csv_url % {'date': date}
log.debug("Loading module list URL (%s)..." % url)
try:
  for line in urlopen(url).readlines():
    line = line.rstrip()
    bits = line.split(',')
    if len(bits) < 3:
      continue
    dll, pdb, uuid = bits[:3]
    if pdb and uuid:
      modules[pdb].add(uuid)
except IOError:
  log.exception("Error fetching symbols")
  sys.exit(1)

symbol_path = config.symbol_path

log.debug("Fetching symbols")
total = sum(len(ids) for ids in modules.values())
current = 0
blacklist_count = 0
skiplist_count = 0
existing_count = 0
not_found_count = 0
file_index = []
# Now try to fetch all the unknown modules from the symbol server
for filename, ids in modules.iteritems():
  # Sometimes we get non-ascii in here. This is definitely not
  # correct, but it should at least stop us from throwing.
  filename = filename.encode('ascii', 'replace')

  if filename.lower() in blacklist:
    # This is one of our our debug files from Firefox/Thunderbird/etc
    current += len(ids)
    blacklist_count += len(ids)
    continue
  for id in ids:
    current += 1
    if verbose:
      sys.stdout.write("[%6d/%6d] %3d%% %-20s\r" % (current, total,
                                                    int(100 * current / total),
                                                    filename[:20]))
    if id in skiplist and skiplist[id] == filename.lower():
      # We've asked the symbol server previously about this, so skip it.
      log.debug("%s/%s already in skiplist", filename, id)
      skiplist_count += 1
      continue
    rel_path = os.path.join(filename, id,
                            filename.replace(".pdb","") + ".sym")
    sym_file = os.path.join(symbol_path, rel_path)
    if os.path.exists(sym_file):
      # We already have this symbol
      log.debug("%s/%s already present", filename, id)
      existing_count += 1
      continue
    if config.read_only_symbol_path != '' and \
       os.path.exists(os.path.join(config.read_only_symbol_path, filename, id,
                                   filename.replace(".pdb","") + ".sym")):
      # We already have this symbol
      log.debug("%s/%s already present", filename, id)
      existing_count += 1
      continue
    # Not in the blacklist, skiplist, and we don't already have it, so
    # ask the symbol server for it.
    # This expects that symsrv_convert.exe and all its dependencies
    # are in the current directory.
    #TODO: make symsrv_convert write to stdout, build zip using ZipFile
    symsrv_convert = os.path.join(thisdir, "symsrv_convert.sh")
    proc = subprocess.Popen(["bash", symsrv_convert,
                             MICROSOFT_SYMBOL_SERVER,
                             symbol_path,
                             filename,
                             id],
                            stdout = subprocess.PIPE,
                            stderr = subprocess.STDOUT)
    # kind of lame, want to prevent it from running too long
    start = time.time()
    # 30 seconds should be more than enough time
    while proc.poll() is None and (time.time() - start) < 30:
      time.sleep(1)
    if proc.poll() is None:
      # kill it, it's been too long
      log.debug("Timed out downloading %s/%s", filename, id)
      proc.kill()
    elif proc.returncode != 0:
      not_found_count += 1
      # Don't skiplist this symbol if we've previously downloaded
      # other symbol versions for the same file. It's likely we'll
      # be able to download it at some point
      if not (os.path.exists(os.path.join(symbol_path, filename)) or
              os.path.exists(os.path.join(config.read_only_symbol_path, filename))):
        log.debug("Couldn't fetch %s/%s, adding to skiplist", filename, id)
        skiplist[id] = filename
      else:
        log.debug("Couldn't fetch %s/%s, but not skiplisting", filename, id)
      convert_output = proc.stdout.read().strip()
      log.debug("symsrv_convert.exe output: '%s'", convert_output)
    if os.path.exists(sym_file):
      log.debug("Successfully downloaded %s/%s", filename, id)
      file_index.append(rel_path.replace("\\", "/"))

if verbose:
  sys.stdout.write("\n")

if not file_index:
  log.info("No symbols downloaded: %d considered, %d already present, %d in blacklist, %d skipped, %d not found"
           % (total, existing_count, blacklist_count, skiplist_count, not_found_count))
  write_skiplist()
  sys.exit(0)

# Write an index file
buildid = time.strftime("%Y%m%d%H%M%S", time.localtime())
index_filename = "microsoftsyms-1.0-WINNT-%s-symbols.txt" % buildid
log.debug("Adding %s" % index_filename)
with open(os.path.join(symbol_path, index_filename), 'w') as f:
  f.write("\n".join(file_index))

# Write out our new skip list
write_skiplist()

log.info("Uploaded %d symbol files" % len(file_index))
log.info("Finished, exiting")
