#!/usr/bin/env python3
# Reversible, guarded Lumen boot.lua shim.
import argparse
from pathlib import Path
import sys
import hashlib
import os

MARK = '-- SLS-ADDONS-INTEGRATION: 2026-10'
INSERT = '''-- SLS-ADDONS-INTEGRATION: 2026-10
local sls_addons = nil
do
  local home = os.getenv('HOME') or ''
  local path = home .. '/.local/share/Lumen/sls-addons/bridge.lua'
  local ok, result = pcall(dofile, path)
  if ok and type(result) == 'table' and type(result.register) == 'function' then
    local registered, err = pcall(result.register, registry)
    if registered then sls_addons = result
    else io.stderr:write('[sls-addons] registry failed: ' .. tostring(err) .. '\\n') end
  elseif not ok then
    io.stderr:write('[sls-addons] bootstrap failed: ' .. tostring(result) .. '\\n')
  end
end
local function sls_add_ui_assets(assets)
  if sls_addons and type(sls_addons.ui_js) == 'table' then
    for _, js in ipairs(sls_addons.ui_js) do
      assets.js[#assets.js + 1] = js
    end
  end
  return assets
end
local sls_control_js = {}
if sls_addons and type(sls_addons.control_js) == 'table' then
  for _, js in ipairs(sls_addons.control_js) do
    sls_control_js[#sls_control_js + 1] = js
  end
end
'''

# Match only the inspected upstream swwayps/lumen boot layout.
ANCHOR='local lua_dir = os.getenv("LUMEN_LUA_DIR") or "lua"'
MENU='return { polyfill = true, css = {}, js = js }'
WEB='return base\nend\n\nlocal loop = require("loop")'
CONTROL='''assets = { polyfill = nil, css = {},
        js = {
          gamepad_toast_js,
          auto_fix_launch_guard_js,
        } }'''
CONTROL_NEW='''assets = { polyfill = (sls_addons ~= nil), css = {},
        js = (function()
          local js = { gamepad_toast_js, auto_fix_launch_guard_js }
          for _, addon in ipairs(sls_control_js) do js[#js + 1] = addon end
          return js
        end)() }'''

def patch(source):
    if MARK in source: return source
    candidates=[(ANCHOR, ANCHOR+'\n'+INSERT),
      (MENU, 'return sls_add_ui_assets({ polyfill = true, css = {}, js = js })'),
      (WEB, 'return sls_add_ui_assets(base)\nend\n\nlocal loop = require("loop")'),
      (CONTROL, CONTROL_NEW)]
    for old,_ in candidates:
      if source.count(old)!=1: raise ValueError(f'Lumen upstream changed: expected one anchor: {old[:45]!r}')
    for old,new in candidates: source=source.replace(old,new,1)
    return source

def main():
    p=argparse.ArgumentParser()
    p.add_argument('mode',choices=['install','uninstall','check'])
    p.add_argument('--lumen',type=Path,default=Path.home()/'.local/share/Lumen')
    opts=p.parse_args()
    dest=opts.lumen/'lua'/'boot.lua'
    backup=dest.with_name('boot.lua.sls-addons.original')
    if not dest.is_file(): sys.exit('Lumen lua/boot.lua missing. Not modifying anything.')
    content=dest.read_text()
    if opts.mode=='check':
      if MARK not in content: patch(content)  # fail if upstream layout changed
      print('bridge installed' if MARK in content else 'bridge compatible, not installed')
      return
    if opts.mode=='uninstall':
      if MARK not in content: print('Lumen boot already unmodified'); return
      if not backup.is_file(): sys.exit('No original backup: refusing unsafe rollback')
      original=backup.read_text()
      if MARK in original: sys.exit('Backup appears patched: refusing rollback')
      if patch(original)!=content: sys.exit('Lumen changed after bridge install: refusing to overwrite; restore manually from backup')
      tmp=dest.with_name('boot.lua.sls-addons.restore.tmp')
      tmp.write_text(original);os.replace(tmp,dest)
      print('Lumen boot restored from backup')
      return
    if MARK in content:
      print('bridge already installed'); return
    updated=patch(content) # fail before modifying disk
    if backup.exists() and backup.read_text()!=content:
      # A LuaTools/Lumen update replaced boot.lua. Preserve the old original,
      # then back up this new, already-verified upstream version.
      digest=hashlib.sha256(backup.read_bytes()).hexdigest()[:12]
      archived=backup.with_name(backup.name+'.'+digest)
      if archived.exists(): sys.exit('An archived backup with this hash already exists')
      os.replace(backup,archived)
    backup.write_text(content)
    tmp=dest.with_name('boot.lua.sls-addons.pending.tmp')
    try:
      tmp.write_text(updated)
      os.replace(tmp,dest)
    finally:
      if tmp.exists(): tmp.unlink()
    print('Lumen bridge installed; original: '+str(backup))

if __name__=='__main__': main()
