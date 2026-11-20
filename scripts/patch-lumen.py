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

# Lumen's own About-page updater would replace the Lua tree and wipe the Luna
# bridge. Lumen updates are controlled from the Luna menu instead (`luna lumen
# update`), so the stock updater is turned into a pointer to it.
BLOCK_BEGIN='-- LUNA-LUMEN-UPDATE-BLOCK: begin v1'
BLOCK_END='-- LUNA-LUMEN-UPDATE-BLOCK: end v1'
BLOCK=BLOCK_BEGIN+'''
function about.update_script(...)
  error('Lumen updates are managed from the Luna menu (Health > Lumen). Update there so Luna stays attached.')
end
'''+BLOCK_END+'\n'
ABOUT_ANCHOR='\nreturn about'

HOOK_BEGIN='-- LUNA-POST-UPDATE-HOOK: begin v1'
HOOK_END='-- LUNA-POST-UPDATE-HOOK: end v1\n'

def drop_old_hook(text):
    """The old update-recovery decorator is superseded by the Luna-menu update."""
    if HOOK_BEGIN not in text: return text
    if text.count(HOOK_BEGIN)!=1 or text.count(HOOK_END)!=1 or text.index(HOOK_BEGIN)>text.index(HOOK_END):
        raise ValueError('old Luna update hook was modified; refusing to remove it')
    return text[:text.index(HOOK_BEGIN)]+text[text.index(HOOK_END)+len(HOOK_END):]

def about_patch(text):
    if BLOCK_BEGIN in text:
        return text
    text=drop_old_hook(text)
    if text.count('function about.update_script(')!=1 or text.count(ABOUT_ANCHOR)!=1:
        raise ValueError('unsupported Lumen about.lua update contract')
    return text.replace(ABOUT_ANCHOR,'\n'+BLOCK+'return about',1)

def about_unpatch(text):
    if BLOCK_BEGIN not in text: return text
    if text.count(BLOCK)!=1: raise ValueError('Lumen update block was modified; refusing to remove it')
    return text.replace('\n'+BLOCK+'return about',ABOUT_ANCHOR,1)

def about_step(mode,lumen):
    """Best effort: a Lumen without the expected about.lua just keeps its own updater."""
    path=lumen/'lua'/'about.lua'
    if not path.is_file(): return
    text=path.read_text()
    try:
        new=about_unpatch(drop_old_hook(text)) if mode=='uninstall' else about_patch(text)
    except ValueError as error:
        print('Lumen self-update NOT blocked: '+str(error),file=sys.stderr); return
    if mode=='check':
        print('lumen self-update blocked' if BLOCK_BEGIN in text else 'lumen self-update open'); return
    if new==text: return
    tmp=path.with_name('about.lua.luna.tmp')
    tmp.write_text(new); os.replace(tmp,path)
    print('Lumen self-update '+('unblocked' if mode=='uninstall' else 'blocked (use the Luna menu)'))

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
      about_step('check',opts.lumen)
      return
    if opts.mode=='uninstall':
      about_step('uninstall',opts.lumen)
      if MARK not in content: print('Lumen boot already unmodified'); return
      if not backup.is_file(): sys.exit('No original backup: refusing unsafe rollback')
      original=backup.read_text()
      if MARK in original: sys.exit('Backup appears patched: refusing rollback')
      if patch(original)!=content: sys.exit('Lumen changed after bridge install: refusing to overwrite; restore manually from backup')
      tmp=dest.with_name('boot.lua.sls-addons.restore.tmp')
      tmp.write_text(original);os.replace(tmp,dest)
      print('Lumen boot restored from backup')
      return
    about_step('install',opts.lumen)
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
