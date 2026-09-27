GPG-encrypted Ansible vault passwords, one per vault id (`dev.gpg`, ...).
Read by `/usr/local/bin/gpg-vault-client` inside the daedalus, using your
forwarded host gpg-agent, so the plaintext never touches disk.

    pwgen -s 48 1 | gpg --encrypt -r you@example.com -r teammate@example.com > .vault-pass/dev.gpg
    make vault-edit                       # VAULT_ID=dev@/usr/local/bin/gpg-vault-client

Production uses OpenBao instead (kv path secret/ansible/vault/prod, field password):

    bao login -method=oidc
    make deploy VAULT_ID=prod@/usr/local/bin/bao-vault-client
