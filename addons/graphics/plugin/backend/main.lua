-- SLS Luna Graphics backend (read-only).
--
-- Reports what the machine has for frame generation and upscaling management:
-- the GPU and driver, the Vulkan layers present (lsfg-vk), its user config, the
-- Decky plugins that overlap with this area, OptiScaler / OptiScaler Client
-- installs, and the Steam runtime facts. Nothing here installs, downloads or
-- changes anything - installers come later, behind their own gates.
local fs = require('fs')
local json = require('json')
local M = {}
local VERSION = '0.1.0'
local home = os.getenv('HOME') or ''
local sysfs_root = os.getenv('SLS_GRAPHICS_SYSFS') or '/sys'

local function read_file(path)
  local f = io.open(path, 'rb')
  if not f then return nil end
  local raw = f:read('*a')
  f:close()
  return raw
end

local function trim(text)
  return tostring(text or ''):gsub('^%s+', ''):gsub('%s+$', '')
end

local function file_info(path)
  local f = io.open(path, 'rb')
  if not f then return nil end
  local size = f:seek('end')
  f:close()
  return { path = path, size = size or 0 }
end

-- ------------------------------------------------------------------ system

function M.parse_os_release(text)
  local out = {}
  for line in tostring(text or ''):gmatch('[^\r\n]+') do
    local key, value = line:match('^([%w_]+)=(.*)$')
    if key then out[key] = trim((value:gsub('^"', ''):gsub('"$', ''))) end
  end
  return out
end

local VENDOR_NAMES = {
  ['0x1002'] = 'AMD',
  ['0x10de'] = 'NVIDIA',
  ['0x8086'] = 'Intel',
  ['0x13b5'] = 'ARM',
  ['0x5143'] = 'Qualcomm',
}

-- "0x1002" -> "0x1002"; strips whitespace/newlines from sysfs values.
local function clean_hex(value)
  local cleaned = trim(value):lower()
  if cleaned == '' then return nil end
  if not cleaned:match('^0x%x+$') then cleaned = '0x' .. cleaned:gsub('^0x', '') end
  return cleaned
end

-- scan(root, home) -> array of { card, vendor, vendor_name, device, driver, product }
-- `root` is a sysfs root (overridable for tests).
function M.gpu_report(root)
  root = root or sysfs_root
  local drm = root .. '/class/drm'
  local cards = {}
  if fs.exists(drm) then
    local ok, items = pcall(fs.list, drm)
    if ok and type(items) == 'table' then
      for _, item in ipairs(items) do
        local name = tostring(item.name or '')
        if item.is_directory and name:match('^card%d+$') then cards[#cards + 1] = name end
      end
    end
  end
  table.sort(cards)
  local out = {}
  for _, card in ipairs(cards) do
    local device_dir = drm .. '/' .. card .. '/device'
    local vendor = clean_hex(read_file(device_dir .. '/vendor'))
    if vendor then
      local uevent = read_file(device_dir .. '/uevent') or ''
      local driver = uevent:match('DRIVER=([^\n]+)')
      out[#out + 1] = {
        card = card,
        vendor = vendor,
        vendor_name = VENDOR_NAMES[vendor] or 'unknown',
        device = clean_hex(read_file(device_dir .. '/device')) or '',
        driver = trim(driver or ''),
        product = trim(read_file(device_dir .. '/product_name') or ''),
      }
    end
  end
  return out
end

-- ------------------------------------------------------------------ lsfg-vk

-- System layer directories are overridable so the detection stays testable on
-- hosts that already have lsfg-vk installed.
local function system_layer_dirs()
  local override = os.getenv('SLS_GRAPHICS_SYSTEM_LAYER_DIRS')
  if override ~= nil then
    local out = {}
    for dir in override:gmatch('[^:]+') do out[#out + 1] = dir end
    return out
  end
  return {
    '/usr/share/vulkan/implicit_layer.d',
    '/usr/local/share/vulkan/implicit_layer.d',
    '/etc/vulkan/implicit_layer.d',
  }
end

-- A layer belongs to lsfg-vk when its file name or manifest mentions it.
local function layer_is_lsfg(name, content)
  if tostring(name):lower():find('lsfg', 1, true) then return true end
  return tostring(content or ''):lower():find('lsfg', 1, true) ~= nil
end

-- lsfg(home_dir, system_dirs) -> { layers = {...}, config = {...} | nil,
-- decky_plugins = {...} }.
function M.lsfg_report(home_dir, system_dirs)
  home_dir = home_dir or home
  local report = { layers = {}, config = nil, decky_plugins = {} }
  local dirs = { home_dir .. '/.local/share/vulkan/implicit_layer.d' }
  for _, dir in ipairs(system_dirs or system_layer_dirs()) do dirs[#dirs + 1] = dir end
  for _, dir in ipairs(dirs) do
    local ok, items = pcall(fs.list, dir)
    if ok and type(items) == 'table' then
      for _, item in ipairs(items) do
        local name = tostring(item.name or '')
        if item.is_directory == false and name ~= '' then
          local content = read_file(dir .. '/' .. name)
          if content and layer_is_lsfg(name, content) then
            local version = content:match('"implementation_version"%s*:%s*(%d+)')
            report.layers[#report.layers + 1] = {
              path = dir .. '/' .. name,
              version = version and tonumber(version) or nil,
            }
          end
        end
      end
    end
  end
  local configCandidates = {
    home_dir .. '/.config/lsfg-vk/conf.toml',
    home_dir .. '/.config/lsfg-vk/config.toml',
  }
  for _, path in ipairs(configCandidates) do
    local info = file_info(path)
    if info then report.config = info; break end
  end
  local pluginCandidates = {
    home_dir .. '/homebrew/plugins/decky-lsfg-vk',
    home_dir .. '/homebrew/plugins/Decky-Framegen',
  }
  for _, path in ipairs(pluginCandidates) do
    if fs.exists(path) then report.decky_plugins[#report.decky_plugins + 1] = path end
  end
  return report
end

-- ------------------------------------------------------------------ optiscaler

-- OptiScaler is deployed per game (next to the game's executable) and managed
-- by OptiScaler Client; Luna can only look for the management installs and the
-- known user directories. Per-game detection is a later, larger job.
function M.optiscaler_report(home_dir)
  home_dir = home_dir or home
  local candidates = {
    { kind = 'client', path = home_dir .. '/.local/share/OptiScalerClient' },
    { kind = 'client', path = home_dir .. '/Applications/OptiScalerClient' },
    { kind = 'client', path = home_dir .. '/.local/bin/OptiScalerClient' },
    { kind = 'client', path = home_dir .. '/opt/OptiScalerClient' },
    { kind = 'files', path = home_dir .. '/.local/share/OptiScaler' },
    { kind = 'files', path = home_dir .. '/.config/optiscaler' },
    { kind = 'files', path = home_dir .. '/.config/OptiScaler' },
  }
  local found = {}
  for _, candidate in ipairs(candidates) do
    if fs.exists(candidate.path) then
      found[#found + 1] = { kind = candidate.kind, path = candidate.path }
    end
  end
  return found
end

-- ------------------------------------------------------------------ steam

function M.steam_root(home_dir)
  for _, candidate in ipairs({
    home_dir .. '/.steam/steam',
    home_dir .. '/.local/share/Steam',
    home_dir .. '/.steam/debian-installation',
  }) do
    if fs.exists(candidate .. '/steam.sh') then return candidate end
  end
  return nil
end

local SLR_TREES = {
  'SteamLinuxRuntime_sniper',
  'SteamLinuxRuntime_soldier',
  'SteamLinuxRuntime',
}

function M.steam_report(home_dir)
  home_dir = home_dir or home
  local root = M.steam_root(home_dir)
  local report = { root = root or '', runtimes = {}, gamescope = false }
  if root then
    for _, name in ipairs(SLR_TREES) do
      if fs.exists(root .. '/steamapps/common/' .. name) then
        report.runtimes[#report.runtimes + 1] = name
      end
    end
  end
  report.gamescope = fs.exists('/usr/bin/gamescope') or fs.exists('/usr/local/bin/gamescope')
  return report
end

-- ------------------------------------------------------------------ registry

function M.register(registry)
  registry.SlsGraphicsGet = function()
    local os_release = M.parse_os_release(read_file(
      os.getenv('SLS_GRAPHICS_OS_RELEASE') or '/etc/os-release'))
    local kernel = trim(read_file('/proc/sys/kernel/osrelease') or '')
    return json.encode({
      success = true,
      version = VERSION,
      system = {
        distro = os_release.PRETTY_NAME or os_release.NAME or '',
        kernel = kernel,
      },
      gpus = M.gpu_report(),
      lsfg = M.lsfg_report(home),
      optiscaler = M.optiscaler_report(home),
      steam = M.steam_report(home),
    })
  end
end

return M
