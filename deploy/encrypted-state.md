# Encrypted state for a new server installation

The optional Compose overlay binds `/srv/state` to a host LUKS filesystem. Both state consumers check the mapped device and a filesystem identity before starting. A missing bind path, an unmounted path, a different device, or the old named volume stops startup. The check also runs on Docker automatic restarts. The ordinary named-volume installation remains the default.

Prepare an encrypted filesystem and mount it on an empty host directory before setup. Keep its unlock key outside the checkout, the image, Docker volumes, and backups of the ciphertext. The operator owns the unlock mechanism and must arrange for the filesystem to be unlocked and mounted before Docker starts the stack. The application never receives the key. Use `cryptsetup luksFormat`, `cryptsetup open`, a filesystem tool, and `mount` under the operator's own key custody policy. An interactive unlock at boot is a valid choice; a key file on the same unencrypted disk is not an at-rest protection boundary.

Once mounted, create a non-secret identity on that filesystem:

```sh
sudo bash deploy/encrypted-state.sh init /absolute/path/to/mounted-state /dev/mapper/daedalus-state
```

Put the printed identity and the mounted directory in `.env` using the four variables shown in `deploy/env.example`. `DAEDALUS_ENCRYPTED_STATE_REQUIRED=1` is essential: it makes the base Compose file fail closed if someone omits the overlay. Check the mount before startup:

```sh
bash deploy/encrypted-state.sh check /absolute/path/to/mounted-state /dev/mapper/daedalus-state 32-lowercase-hex-digits
docker compose -f deploy/compose.yaml -f deploy/compose.encrypted-state.yaml --env-file .env up -d --build
```

`deploy/setup.sh` checks the mount and selects the overlay for a new installation. It refuses encryption opt-in when the Compose project's named state volume already exists. An existing installation needs a separate stopped-service migration with a WAL-consistent SQLite backup, complete blob and state inventory, restore verification, and a rollback plan. This overlay does not move existing state. Keep the old volume intact until that migration is proven.

For backup, stop state writers, make a consistent database backup, unmount and close the mapping, then copy the ciphertext device or image while it is closed. Test restoration to a separate mapping and check database integrity, history, snapshots, and blobs before relying on the backup. Keep the unlock key in separate custody; a ciphertext copy without a recoverable key is not a usable backup. A backup of mounted plaintext needs its own encrypted destination.

This option covers only `/srv/state`. Workspaces, worktrees, terminal and browser volumes, host logs, swap, and other backups require their own at-rest controls. It does not prevent a privileged host user or an authorized running process from reading mounted state.
