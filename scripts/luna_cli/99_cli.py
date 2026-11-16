
def build_parser():
    parser = argparse.ArgumentParser(prog='luna', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command')
    for name, handler in (('preflight', cmd_preflight), ('status', cmd_status),
                          ('install', cmd_install), ('uninstall', cmd_uninstall),
                          ('verify', cmd_verify), ('vendor', cmd_vendor),
                          ('release', cmd_release), ('update', cmd_update),
                          ('signing', cmd_signing),
                          ('check', cmd_check), ('autoupdate', cmd_autoupdate),
                          ('reconcile', cmd_reconcile),
                          ('backup', cmd_backup), ('restore', cmd_restore),
                          ('steam', cmd_steam), ('proton', cmd_proton),
                          ('refresh', cmd_refresh),
                          ('probe', cmd_probe), ('fleet', cmd_fleet), ('games', cmd_games),
                          ('launchers', cmd_launchers),
                          ('doctor', cmd_doctor), ('gui', cmd_gui),
                          ('library-order', cmd_library_order),
                          ('saves', cmd_saves),
                           ('timer', cmd_timer), ('hook', cmd_hook)):
        child = sub.add_parser(name)
        child.set_defaults(handler=handler)
        child.add_argument('--only', action='append', default=[],
                           help='restrict to one add-on (repeatable): %s' % ', '.join(ADDONS))
        if name == 'install':
            child.add_argument('--force', action='store_true',
                               help='replace installed files that differ from the pinned payloads')
            child.add_argument('--dry-run', action='store_true', help='print the plan and stop')
            child.add_argument('--keep-steam-client', action='store_true',
                               default=os.environ.get('LUNA_KEEP_STEAM_CLIENT') == '1',
                               help='skip the default verified Steam compatibility downgrade')
            child.add_argument('--no-steam-client', action='store_true',
                               help='never touch the Steam client (same as --keep-steam-client)')
            child.add_argument('--steam-client', action='store_true',
                               help='force the Steam client install/verify even with --only or a matching pin')
            child.add_argument('--client-cache', metavar='DIR',
                               default=os.environ.get('LUNA_STEAM_CACHE') or None,
                               help='extra local directory of Steam client packages checked (sha256) '
                                    'before downloading; also LUNA_STEAM_CACHE')
            child.add_argument('--no-steam-machine', action='store_true',
                               help='do not generate SteamOS Mode application shortcuts')
            child.add_argument('--migrate-steam-machine', action='store_true',
                               help='back up, rename and upgrade recognized legacy launchers to SteamOS Mode')
        if name == 'doctor':
            child.add_argument('--json', action='store_true', help='machine-readable findings')
            child.add_argument('--fix', action='store_true',
                               help='apply only safe, reversible fixes (never sudo, never overwrites payloads)')
        if name == 'status':
            child.add_argument('--live', action='store_true',
                               help='also probe the RUNNING Steam client (read-only, through its DevTools '
                                    'port): backend health, boot.lua marker, stale Lumen sidecar')
        if name == 'library-order':
            child.add_argument('--json', action='store_true', help='machine-readable output')
            child.add_argument('--save', metavar='FILE', help='write the complete order to FILE')
            child.add_argument('--compare', metavar='FILE', help='compare with a saved order (exit 1 if different)')
            child.add_argument('--limit', type=int, default=40, metavar='N',
                               help='rows to print (default 40; 0 = all)')
        if name == 'gui':
            child.add_argument('gui_action', nargs='?', default='status',
                               choices=['status', 'launch', 'verify', 'library', 'watch', 'restart-check'],
                               help='status (default), launch Steam in accessible mode, verify add-on panels, '
                                    'read the Library list order, watch an element appear/vanish, or run the Steam restart-cycle '
                                    'check. Exit codes of verify/library/launch: '
                                    '0 pass, 1 failure, 5 skipped (the check could not run)')
            child.add_argument('--json', action='store_true', help='with status/library/verify: machine-readable output')
            child.add_argument('--launch', action='store_true',
                               help='with verify: start Steam in accessible mode first if it is not running')
            child.add_argument('--wait', type=int, default=90, metavar='SECONDS',
                               help='with verify: how long to wait for the Steam window (default 90)')
            child.add_argument('--no-navigate', action='store_true',
                               help='with verify: do not open the Luna overlay (only the entry point is checked)')
            child.add_argument('--open', action='store_true',
                               help='with library: click the Library tab first if it is not showing')
            child.add_argument('--save', metavar='FILE', help='with library: save the order to FILE (JSON)')
            child.add_argument('--compare', metavar='FILE',
                               help='with library: compare with a saved order; exit 1 when it differs')
            child.add_argument('--common', action='store_true',
                               help='with library --compare: only the relative order of names in both lists')
            child.add_argument('--report', metavar='FILE', help='with verify: markdown report path')
            child.add_argument('--screenshots', metavar='DIR', help='with verify: screenshot directory')
            child.add_argument('--keep-steam', action='store_true',
                               help='with verify --launch / restart-check: leave Steam as it ends up instead of '
                                    'returning it to the state it was in before')
            child.add_argument('--force', action='store_true',
                               help='with restart-check: continue although a game is running or a download is active')
            child.add_argument('--final', choices=['accessible', 'normal', 'stopped'],
                               help='with restart-check: how to leave Steam (default: the mode it started in; '
                                    'normal costs one more restart)')
            child.add_argument('--shutdown-timeout', type=int, default=90, metavar='SECONDS',
                               help='with restart-check: wait this long for Steam to exit (default 90)')
            child.add_argument('--relaunch-wait', type=int, default=240, metavar='SECONDS',
                               help='with restart-check: wait this long for the relaunched window (default 240)')
            child.add_argument('--settle', type=int, default=90, metavar='SECONDS',
                               help='with restart-check: after the relaunch, wait up to this long for the Library order to come up (default 90)')
            child.add_argument('--login-grace', type=int, default=GUI_LOGIN_GRACE, metavar='SECONDS',
                               help='how long a "Sign in to Steam" window may stay up, with no log-on attempt in '
                                    'connection_log, before it counts as the real sign-in form (default %d; Steam shows '
                                    'the same window while it auto-logs-on, which took ~37 s)' % GUI_LOGIN_GRACE)
            child.add_argument('--role', help='with watch: accessibility role to watch (e.g. button)')
            child.add_argument('--name', help='with watch: element name (exact unless --contains)')
            child.add_argument('--contains', action='store_true', help='with watch: --name matches as a substring')
            child.add_argument('--addon', metavar='KEY',
                               help='with watch: use the panel selector of an add-on/page key (e.g. playtime, hltb)')
            child.add_argument('--interval', type=float, default=2.0, metavar='SECONDS',
                               help='with watch: seconds between samples (default 2; min 0.2)')
            child.add_argument('--duration', type=int, metavar='SECONDS',
                               help='with watch: stop after this many seconds (default: until Ctrl-C)')
            child.add_argument('--fail-if-vanished', action='store_true',
                               help='with watch: stop and exit 1 when the element vanishes after having been seen')
            child.add_argument('--full-screen', action='store_true',
                               help='with verify: capture the WHOLE screen with grim (everything visible, '
                                    'including unrelated windows); default is the Steam window only')
        if name == 'refresh':
            child.add_argument('--dry-run', action='store_true',
                               help='print the plan without updating or writing')
        if name == 'launchers':
            child.add_argument('--dry-run', action='store_true', help='show launcher changes without writing')
            child.add_argument('--migrate-legacy', action='store_true',
                               help='back up and replace recognized legacy Gamescope+Steam launchers')
        if name == 'reconcile':
            child.add_argument('--dry-run', action='store_true',
                               help='report what would merge without writing')
        if name == 'backup':
            child.add_argument('--out', help='output directory (default: system temp dir)')
        if name == 'restore':
            child.add_argument('archive', help='backup archive to restore')
            child.add_argument('--force', action='store_true',
                               help='overwrite files that are newer than the backup')
        if name == 'steam':
            child.add_argument('steam_action', nargs='?', default='status',
                               choices=['status', 'pin', 'unpin', 'cache', 'recover',
                                        'update', 'cancel', 'lock', 'unlock'],
                               help='what to do (default: status)')
            child.add_argument('--force', action='store_true',
                               help='with lock: kill a running Steam without asking')
            child.add_argument('--adopt', action='store_true',
                               help='with lock: only record the hard lock for the current pinned build '
                                    'when package/ is already read-only and matches the pin '
                                    '(no rollback, Steam not touched)')
            child.add_argument('--relaunch', action='store_true',
                               help='with lock/unlock: notify the result and reopen Steam detached '
                                    '(unlock closes a running Steam first); for the in-Steam UI')
            child.add_argument('--build', help='client build for pin/cache/recover (default: detected/pinned)')
            child.add_argument('--now', action='store_true',
                               help='with update: apply right away instead of queueing (Steam must be closed); '
                                    'with unlock: launch Steam so it self-updates')
        if name == 'proton':
            child.add_argument('proton_action', nargs='?', default='status',
                               choices=['status', 'install', 'remove', 'prune', 'verify'],
                               help='what to do (default: status)')
            child.add_argument('proton_version', nargs='?', help='GE-Proton version (install: default latest)')
            child.add_argument('--json', action='store_true', help='with status: machine-readable output')
            child.add_argument('--dry-run', action='store_true', help='report only; change nothing')
            child.add_argument('--force', action='store_true', help='with remove: even if games are mapped to it (never overrides a running Steam)')
            child.add_argument('--keep', type=int, default=1, help='with prune: newest GE-Proton to keep (default 1)')
            child.add_argument('--rebuild', action='store_true',
                               help='with verify VERSION: create the manifest from a re-downloaded, sha512-verified '
                                    'release, then check the install against it')
            child.add_argument('--against', metavar='FILE',
                               help='with verify VERSION: compare with a sha256sum-format reference file (read-only)')
        if name == 'probe':
            child.add_argument('--json', action='store_true', help='one-line JSON (used by luna fleet)')
        if name == 'fleet':
            child.add_argument('fleet_action', choices=['add', 'remove', 'list', 'status', 'gui', 'push-cache', 'install'],
                               help='fleet action')
            child.add_argument('hosts', nargs='*', help='ssh host name(s) from ~/.ssh/config or user@host')
            child.add_argument('--json', action='store_true', help='with status/gui: machine-readable output')
            child.add_argument('--verify', action='store_true',
                               help='with gui: also run `luna gui verify` on hosts whose Steam is running in '
                                    'accessible mode (navigates the remote UI, takes a window screenshot)')
            child.add_argument('--dry-run', action='store_true', help='with install/push-cache: report only')
            child.add_argument('--restart-steam', action='store_true',
                               help='with install: close Steam gracefully first and relaunch it afterwards')
            child.add_argument('--force', action='store_true',
                               help='with install: overwrite drifted payloads (and old Luna builds)')
            child.add_argument('--trust-signing-key', action='store_true',
                               help='with install: after a successful install, make the host trust this '
                                    'machine\'s release public key (luna signing trust over ssh)')
        if name == 'games':
            child.add_argument('games_action', choices=['list', 'diff', 'install'], help='games action')
            child.add_argument('games_args', nargs='*', metavar='HOST/APPID',
                               help='diff [HOST]; install HOST APPID...')
            child.add_argument('--json', action='store_true', help='with list/diff: machine-readable output')
            child.add_argument('--dry-run', action='store_true', help='with install: report only')
            child.add_argument('--start-steam', action='store_true',
                               help='with install: start Steam on the target if it is not running')

        if name == 'saves':
            child.add_argument('saves_action', choices=['scan', 'status', 'link', 'unlink'],
                               help='scan candidates / show links / link / unlink')
            child.add_argument('target', nargs='?', help='appid, store key or save directory path')
            child.add_argument('--all', action='store_true', help='with link/unlink: every game in saves.json')
            child.add_argument('--prefer', choices=['local', 'store'],
                               help='with link: which side wins when both have saves (other side is backed up)')
            child.add_argument('--json', action='store_true', help='with scan/status: machine-readable output')
            child.add_argument('--dry-run', action='store_true', help='with link/unlink: report only')
        if name == 'check':
            child.add_argument('--force', action='store_true', help='ignore the 6h cache')
        if name == 'autoupdate':
            child.add_argument('--verbose', action='store_true', help='print the decision')
        if name == 'timer':
            child.add_argument('action', choices=['on', 'off', 'status'],
                                help='enable, disable or report the unattended-update timer')
        if name == 'hook':
            child.add_argument('action', choices=['on', 'off', 'status', 'repair'],
                               help='manage bridge-only recovery after Lumen updates')
        if name == 'release':
            child.add_argument('--out', help='output directory (default: dist/)')
            child.add_argument('--publish', action='store_true',
                               help='create or refresh a GitHub release with the bundle (needs gh)')
        if name == 'update':
            child.add_argument('--bundle', help='install from a local bundle tarball')
            child.add_argument('--url', help='install from a bundle URL (a .sha256 beside it is verified)')
            child.add_argument('--latest', action='store_true',
                               help='download the newest release asset (needs gh)')
            child.add_argument('--allow-legacy-bundle', action='store_true',
                               help='accept a pre-RELEASE-FILES.json bundle whose scripts/luna.py is unhashed')
            child.add_argument('--allow-unsigned', action='store_true',
                               help='install a bundle that has no signature although this machine trusts a '
                                    'release key (an invalid or untrusted signature is always refused)')
            child.add_argument('--dry-run', action='store_true', help='print the plan and stop')
        if name == 'signing':
            child.add_argument('signing_action', choices=['status', 'init', 'trust', 'untrust'],
                               help='status; init (create this machine\'s release key and trust it); '
                                    'trust PUBKEY_OR_FILE; untrust PUBKEY_OR_FILE_OR_FINGERPRINT')
            child.add_argument('key', nargs='?', help='trust/untrust: ssh-ed25519 public key text, a file, '
                                                      '- for stdin; untrust also takes SHA256:FINGERPRINT')
        if name in ('verify', 'vendor'):
            child.add_argument('--sources', action='store_true',
                               help='also compare against local source checkouts')
            child.add_argument('--source-dir', help='parent directory holding slsteam-playtime / slsteam-presence')
    return parser


def main(argv=None):
    parser = build_parser()
    options = parser.parse_args(argv)
    if not getattr(options, 'handler', None):
        parser.print_help()
        return EXIT_OK
    return options.handler(options)


if __name__ == '__main__':
    sys.exit(main())
