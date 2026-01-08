Distribution packages
=====================

There are some Linux distributions with packages for ara with a varying degree of support and maintenance.

Please feel free to help complete this documentation and contribute to the packaging of ara.

Due to limited time and resources, official project support is only provided for PyPI and limited container images:

- https://pypi.org/project/ara
- https://quay.io/repository/recordsansible/ara-api
- https://hub.docker.com/r/recordsansible/ara-api

Fedora
~~~~~~

There is a package for ara on Fedora:

- https://src.fedoraproject.org/rpms/ara
- https://koji.fedoraproject.org/koji/packageinfo?packageID=24394
- https://bodhi.fedoraproject.org/updates/?packages=ara

Updating and building the package
---------------------------------

At a high level:

.. code-block:: bash

  $ git clone https://src.fedoraproject.org/rpms/ara; cd ara
  # < update ara.spec, bump version, update requirements, changelog, etc >

  # download new source tarball from pypi
  $ spectool -R -g ara.spec

  # produce a source rpm
  $ rpmbuild -bs ara.spec

  # maybe do a scratch build to make sure it works
  $ kinit login@FEDORAPROJECT.ORG

  $ koji build --scratch rawhide /home/user/rpmbuild/SRPMS/ara-1.7.4-1.fc42.src.rpm
  Uploading srpm: /home/user/rpmbuild/SRPMS/ara-1.7.4-1.fc42.src.rpm
  [====================================] 100% 00:00:03  12.52 MiB   3.56 MiB/sec
  Created task: 140842494
  Task info: https://koji.fedoraproject.org/koji/taskinfo?taskID=140842494
  Watching tasks (this may be safely interrupted)...
  140842494 build (rawhide, ara-1.7.4-1.fc42.src.rpm): free
  140842494 build (rawhide, ara-1.7.4-1.fc42.src.rpm): free -> assigned (buildvm-x86-05.rdu3.fedoraproject.org)
  140842494 build (rawhide, ara-1.7.4-1.fc42.src.rpm): assigned (buildvm-x86-05.rdu3.fedoraproject.org) -> open (buildvm-x86-05.rdu3.fedoraproject.org)
    140842497 rebuildSRPM (x86_64): free
    140842497 rebuildSRPM (x86_64): free -> assigned (buildhw-x86-03.rdu3.fedoraproject.org)
    140842497 rebuildSRPM (x86_64): assigned (buildhw-x86-03.rdu3.fedoraproject.org) -> open (buildhw-x86-03.rdu3.fedoraproject.org)
    140842497 rebuildSRPM (x86_64): open (buildhw-x86-03.rdu3.fedoraproject.org) -> closed
    0 free  1 open  1 done  0 failed
    140842503 buildArch (ara-1.7.4-1.fc44.src.rpm, noarch): free
    140842503 buildArch (ara-1.7.4-1.fc44.src.rpm, noarch): free -> assigned (buildhw-a64-03.rdu3.fedoraproject.org)
    140842503 buildArch (ara-1.7.4-1.fc44.src.rpm, noarch): assigned (buildhw-a64-03.rdu3.fedoraproject.org) -> open (buildhw-a64-03.rdu3.fedoraproject.org)
    140842503 buildArch (ara-1.7.4-1.fc44.src.rpm, noarch): open (buildhw-a64-03.rdu3.fedoraproject.org) -> closed
    0 free  1 open  2 done  0 failed
  140842494 build (rawhide, ara-1.7.4-1.fc42.src.rpm): open (buildvm-x86-05.rdu3.fedoraproject.org) -> closed
    0 free  0 open  3 done  0 failed
  140842494 build (rawhide, ara-1.7.4-1.fc42.src.rpm) completed successfully

  # upload the new source tarball
  fedpkg new-sources /home/user/rpmbuild/SOURCES/ara-1.7.4.tar.gz

  # commit, check out to a fork/branch

Once a PR is opened (like `this one <https://src.fedoraproject.org/rpms/ara/pull-request/45>`_) it will run various tests like building and rpmlint.
After successful jobs and review, the PR will be merged and now a build is necessary for the new version to land in repositories.

We must do this for rawhide and supported fedora versions like 42 and 43.

First, check out the desired branch and then ``fedpkg build``.

For rawhide, a successful build will automatically end up in repositories.
For stable releases we must open a bodhi update with ``fedpkg update``.

This will propose the new versions for stable release, pending votes from testers or a certain duration.
Some time after either condition is met, the new package will eventually make its way to fedora mirrors.

Debian
~~~~~~

There are outdated (1.5.8 at time of writing) packages available for Debian bookworm:

- https://packages.debian.org/bookworm/ara-client
- https://packages.debian.org/bookworm/ara-server

Ubuntu
~~~~~~

There are outdated (1.5.8 at time of writing) packages available for Ubuntu 22.04 and 24.04:

- https://packages.ubuntu.com/noble/ara-client
- https://packages.ubuntu.com/noble/ara-server

They are probably inherited from Debian.

Arch
~~~~

There is an outdated (1.7.0 at time of writing) package available for Arch:

- https://aur.archlinux.org/packages/python-ara
