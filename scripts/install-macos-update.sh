#!/bin/sh
# One authorization installs the verified application and cleans temporary files.
set -eu

target=$1
archive=$2
expected_digest=$3
version=$4
identifier=$5
token=$6
caller_uid=$7
parent_pid=$8
case "$token" in macos-update-startup-*) ;; *) exit 1 ;; esac
case "$token" in *[!a-zA-Z0-9-]*|'') exit 1 ;; esac
case "$caller_uid:$parent_pid" in *[!0-9:]*|:*|*:) exit 1 ;; esac
[ "${#expected_digest}" -eq 64 ]
case "$expected_digest" in *[!0-9a-f]*) exit 1 ;; esac
parent=$(dirname "$target")
identity=$(printf '%s' "$target" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}')
legacy="$parent/.pymss-studio-previous-$identity.app"
control="/private/tmp/pymss-studio-authorized-$token"
umask 077
# The root-owned directory in sticky /private/tmp cannot be replaced by the caller.
/bin/mkdir -m 755 "$control"
/bin/mkdir -m 700 "$control/private"
printf 'preparing' > "$control/status"
/bin/chmod 644 "$control/status"
printf '%64s' '' > "$control/outcome"
/usr/sbin/chown "$caller_uid" "$control/outcome"
/bin/chmod 600 "$control/outcome"
stage=
staged=
backup=
phase=preparing

validate_bundle_identity() {
    [ ! -L "$1" ] || return 1
    [ -d "$1" ] || return 1
    [ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$1/Contents/Info.plist")" = "$identifier" ] || return 1
}

validate_bundle() {
    validate_bundle_identity "$1" || return 1
    /usr/bin/codesign --verify --deep --strict --verbose=4 "$1" || {
        echo "Application code signature verification failed: $1" >&2
        return 1
    }
}

replace_application() {
    backup="$stage/obsolete.app"
    /bin/mv "$target" "$backup" || return 1
    /bin/mv "$staged" "$target" || return 1
}

rollback() {
    [ -n "$backup" ] && [ -d "$backup" ] && [ ! -L "$backup" ] || return 1
    if [ -e "$target" ] || [ -L "$target" ]; then
        /bin/mv "$target" "$stage/rejected.app" || return 1
    fi
    /bin/mv "$backup" "$target" || {
        if [ ! -e "$target" ] && [ -d "$stage/rejected.app" ]; then
            /bin/mv "$stage/rejected.app" "$target" || :
        fi
        echo "Unable to restore the previous application. Backup retained at $backup" >&2
        return 1
    }
    phase=aborted
}

cleanup_legacy_stages() {
    for candidate in "$parent"/.pymss-studio-update-*; do
        [ "$candidate" != "$stage" ] || continue
        [ -d "$candidate" ] && [ ! -L "$candidate" ] || continue
        # A marker protects live updaters, not directories from ended or
        # interrupted transactions. Unknown contents are never deleted.
        known=1
        found=0
        for item in "$candidate"/* "$candidate"/.[!.]* "$candidate"/..?*; do
            [ -e "$item" ] || [ -L "$item" ] || continue
            case "$(basename "$item")" in
                .pymss-update-active)
                    [ -f "$item" ] && [ ! -L "$item" ] || { known=0; continue; }
                    marker_pid=$(/usr/bin/head -c 64 "$item" | /usr/bin/awk '{print $1}')
                    expired=$(/usr/bin/find "$item" -mtime +0 -print)
                    case "$marker_pid" in
                        ''|*[!0-9]*) ;;
                        *) if [ -z "$expired" ] && [ "$marker_pid" != "$$" ] && [ "$marker_pid" != "$parent_pid" ] && /bin/kill -0 "$marker_pid" 2>/dev/null; then known=0; fi ;;
                    esac
                    ;;
                'Pymss Studio.app'|obsolete.app)
                    found=1
                    if ! validate_bundle "$item"; then known=0; fi
                    ;;
                *) known=0 ;;
            esac
        done
        if [ "$known" -eq 1 ] && [ "$found" -eq 1 ]; then
            /bin/rm -rf "$candidate" || echo "Unable to clean a previous update directory: $candidate" >&2
        fi
    done
}

finish() {
    status=$?
    trap - 0 HUP INT TERM
    case "$phase" in
        committed|aborted) ;;
        *)
            if [ -n "$backup" ] && [ -d "$backup" ]; then
                if ! rollback; then phase=rollback-failed; fi
            fi
            status=1
            ;;
    esac
    if [ -n "$stage" ] && [ "$phase" != rollback-failed ]; then
        /bin/rm -rf "$stage" || echo "Unable to clean update directory: $stage" >&2
    fi
    /bin/rm -rf "$control/private" || echo "Unable to clean private update files: $control/private" >&2
    if [ "$status" -ne 0 ]; then
        if [ "$phase" = rollback-failed ]; then
            echo "Rollback failed. Previous application retained at $backup" >&2
            printf 'rollback-failed' > "$control/status" || :
        else
            printf 'failed' > "$control/status" || :
        fi
    fi
    i=0
    while [ "$i" -lt 25 ]; do
        [ "$(/usr/bin/head -c 64 "$control/outcome" | /usr/bin/awk '{print $1}')" != observed ] || break
        i=$((i + 1)); /bin/sleep 0.2
    done
    /bin/rm -rf "$control" || echo "Unable to clean update control directory: $control" >&2
    exit "$status"
}
trap finish 0
trap 'exit 1' HUP INT TERM

# Hash and unpack a root-owned snapshot, never the caller's mutable extracted app.
/bin/cp "$archive" "$control/private/update.app.tar.gz"
actual_digest=$(/usr/bin/shasum -a 256 "$control/private/update.app.tar.gz" | /usr/bin/awk '{print $1}')
[ "$actual_digest" = "$expected_digest" ] || { echo 'The verified update archive changed before authorization.' >&2; exit 1; }
/bin/mkdir "$control/private/extracted"
(umask 022; /usr/bin/tar --no-same-owner --no-same-permissions -xzf "$control/private/update.app.tar.gz" -C "$control/private/extracted")
source="$control/private/extracted/Pymss Studio.app"
validate_bundle "$source"
[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$source/Contents/Info.plist")" = "$version" ]
# Only the replacement supplies trusted bytes. An invalid resource seal on the
# installed application must not block replacing it with the verified release.
validate_bundle_identity "$target"
/bin/kill -0 "$parent_pid" 2>/dev/null || exit 1
if [ -e "$legacy" ] || [ -L "$legacy" ]; then
    validate_bundle "$legacy"
    /bin/rm -rf "$legacy"
fi
stage=$(/usr/bin/mktemp -d "$parent/.pymss-studio-update-XXXXXXXX")
printf '%s' "$$" > "$stage/.pymss-update-active"
staged="$stage/Pymss Studio.app"
/usr/bin/ditto "$source" "$staged"
validate_bundle "$staged"

phase=exchanging
replace_application || exit 1
phase=installed
printf 'installed' > "$control/status"

# The caller may write this regular file but cannot replace it with a symlink/FIFO.
i=0
while [ "$i" -lt 600 ]; do
    outcome=$(/usr/bin/head -c 64 "$control/outcome" | /usr/bin/awk '{print $1}')
    case "$outcome" in
        commit)
            cleanup_legacy_stages
            phase=committed
            printf 'committed' > "$control/status"
            exit 0
            ;;
        abort)
            rollback || { phase=rollback-failed; exit 1; }
            printf 'aborted' > "$control/status"
            exit 0
            ;;
        ready:*)
            child_pid=${outcome#ready:}
            case "$child_pid" in ''|*[!0-9]*) exit 1 ;; esac
            if ! /bin/kill -0 "$parent_pid" 2>/dev/null && /bin/kill -0 "$child_pid" 2>/dev/null; then
                cleanup_legacy_stages
                phase=committed
                printf 'committed' > "$control/status"
                exit 0
            fi
            ;;
        '') /bin/kill -0 "$parent_pid" 2>/dev/null || exit 1 ;;
        *) exit 1 ;;
    esac
    i=$((i + 1))
    /bin/sleep 0.2
done

echo 'The application was installed, but startup confirmation timed out.' >&2
exit 1
