"""ssh-guarded scripts package.

These scripts read user intent, produce JSON request artifacts, and only
execute against artifacts (never directly). Transport is delegated to
ssh-core; this package never opens an SSH connection itself.
"""
