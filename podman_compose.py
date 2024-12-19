#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-2.0
# https://docs.docker.com/compose/compose-file/#service-configuration-reference
# https://docs.docker.com/samples/
# https://docs.docker.com/compose/gettingstarted/
# https://docs.docker.com/compose/django/
# https://docs.docker.com/compose/wordpress/
# TODO: podman pod logs --color -n -f pod_testlogs
from __future__ import annotations

import argparse
import asyncio.subprocess
import getpass
import glob
import hashlib
import itertools
import json
import logging
import os
import random
import re
import shlex
import signal
import subprocess
import sys
from asyncio import Task
from enum import Enum

try:
    from shlex import quote as cmd_quote
except ImportError:
    from pipes import quote as cmd_quote  # pylint: disable=deprecated-module

# import fnmatch
# fnmatch.fnmatchcase(env, "*_HOST")

import yaml
from dotenv import dotenv_values

__version__ = "1.2.0"

script = os.path.realpath(sys.argv[0])

# helper functions


def is_list(list_object):
    return (
        not isinstance(list_object, str)
        and not isinstance(list_object, dict)
        and hasattr(list_object, "__iter__")
    )


# identity filter
def filteri(a):
    return filter(lambda i: i, a)


def try_int(i, fallback=None):
    try:
        return int(i)
    except ValueError:
        pass
    except TypeError:
        pass
    return fallback


def try_float(i, fallback=None):
    try:
        return float(i)
    except ValueError:
        pass
    except TypeError:
        pass
    return fallback


log = logging.getLogger(__name__)


dir_re = re.compile(r"^[~/\.]")
propagation_re = re.compile(
    "^(?:z|Z|O|U|r?shared|r?slave|r?private|r?unbindable|r?bind|(?:no)?(?:exec|dev|suid))$"
)
norm_re = re.compile("[^-_a-z0-9]")
num_split_re = re.compile(r"(\d+|\D+)")

PODMAN_CMDS = (
    "pull",
    "push",
    "build",
    "inspect",
    "run",
    "start",
    "stop",
    "rm",
    "volume",
)

t_re = re.compile(r"^(?:(\d+)[m:])?(?:(\d+(?:\.\d+)?)s?)?$")
STOP_GRACE_PERIOD = "10"


def str_to_seconds(txt):
    if not txt:
        return None
    if isinstance(txt, (int, float)):
        return txt
    match = t_re.match(txt.strip())
    if not match:
        return None
    mins, sec = match[1], match[2]
    mins = int(mins) if mins else 0
    sec = float(sec) if sec else 0
    # "podman stop" takes only int
    # Error: invalid argument "3.0" for "-t, --time" flag: strconv.ParseUint: parsing "3.0":
    # invalid syntax
    return int(mins * 60.0 + sec)


def ver_as_list(a):
    return [try_int(i, i) for i in num_split_re.findall(a)]


def strverscmp_lt(a, b):
    a_ls = ver_as_list(a or "")
    b_ls = ver_as_list(b or "")
    return a_ls < b_ls


def parse_short_mount(mount_str, basedir):
    mount_a = mount_str.split(":")
    mount_opt_dict = {}
    mount_opt = None
    if len(mount_a) == 1:
        # Anonymous: Just specify a path and let the engine creates the volume
        # - /var/lib/mysql
        mount_src, mount_dst = None, mount_str
    elif len(mount_a) == 2:
        mount_src, mount_dst = mount_a
        # dest must start with / like /foo:/var/lib/mysql
        # otherwise it's option like /var/lib/mysql:rw
        if not mount_dst.startswith("/"):
            mount_dst, mount_opt = mount_a
            mount_src = None
    elif len(mount_a) == 3:
        mount_src, mount_dst, mount_opt = mount_a
    else:
        raise ValueError("could not parse mount " + mount_str)
    if mount_src and dir_re.match(mount_src):
        # Specify an absolute path mapping
        # - /opt/data:/var/lib/mysql
        # Path on the host, relative to the Compose file
        # - ./cache:/tmp/cache
        # User-relative path
        # - ~/configs:/etc/configs/:ro
        mount_type = "bind"
        if os.name != 'nt' or (os.name == 'nt' and ".sock" not in mount_src):
            mount_src = os.path.abspath(os.path.join(basedir, os.path.expanduser(mount_src)))
    else:
        # Named volume
        # - datavolume:/var/lib/mysql
        mount_type = "volume"
    mount_opts = filteri((mount_opt or "").split(","))
    propagation_opts = []
    for opt in mount_opts:
        if opt == "ro":
            mount_opt_dict["read_only"] = True
        elif opt == "rw":
            mount_opt_dict["read_only"] = False
        elif opt in ("consistent", "delegated", "cached"):
            mount_opt_dict["consistency"] = opt
        elif propagation_re.match(opt):
            propagation_opts.append(opt)
        else:
            # TODO: ignore
            raise ValueError("unknown mount option " + opt)
    mount_opt_dict["bind"] = {"propagation": ",".join(propagation_opts)}
    return {
        "type": mount_type,
        "source": mount_src,
        "target": mount_dst,
        **mount_opt_dict,
    }


# NOTE: if a named volume is used but not defined it
# gives ERROR: Named volume "abc" is used in service "xyz"
#   but no declaration was found in the volumes section.
# unless it's anonymous-volume


def fix_mount_dict(compose, mount_dict, srv_name):
    """
    in-place fix mount dictionary to:
    - define _vol to be the corresponding top-level volume
    - if name is missing it would be source prefixed with project
    - if no source it would be generated
    """
    # if already applied nothing todo
    if "_vol" in mount_dict:
        return mount_dict
    if mount_dict["type"] == "volume":
        vols = compose.vols
        source = mount_dict.get("source")
        vol = (vols.get(source, {}) or {}) if source else {}
        name = vol.get("name")
        mount_dict["_vol"] = vol
        # handle anonymous or implied volume
        if not source:
            # missing source
            vol["name"] = "_".join([
                compose.project_name,
                srv_name,
                hashlib.sha256(mount_dict["target"].encode("utf-8")).hexdigest(),
            ])
        elif not name:
            external = vol.get("external")
            if isinstance(external, dict):
                vol["name"] = external.get("name", f"{source}")
            elif external:
                vol["name"] = f"{source}"
            else:
                vol["name"] = f"{compose.project_name}_{source}"
    return mount_dict


# docker and docker-compose support subset of bash variable substitution
# https://docs.docker.com/compose/compose-file/#variable-substitution
# https://docs.docker.com/compose/env-file/
# https://www.gnu.org/software/bash/manual/html_node/Shell-Parameter-Expansion.html
# $VARIABLE
# ${VARIABLE}
# ${VARIABLE:-default} default if not set or empty
# ${VARIABLE-default} default if not set
# ${VARIABLE:?err} raise error if not set or empty
# ${VARIABLE?err} raise error if not set
# $$ means $

var_re = re.compile(
    r"""
    \$(?:
        (?P<escaped>\$) |
        (?P<named>[_a-zA-Z][_a-zA-Z0-9]*) |
        (?:{
            (?P<braced>[_a-zA-Z][_a-zA-Z0-9]*)
            (?:(?P<empty>:)?(?:
                (?:-(?P<default>[^}]*)) |
                (?:\?(?P<err>[^}]*))
            ))?
        })
    )
""",
    re.VERBOSE,
)


def rec_subs(value, subs_dict):
    """
    do bash-like substitution in value and if list of dictionary do that recursively
    """
    if isinstance(value, dict):
        if 'environment' in value and isinstance(value['environment'], dict):
            # Load service's environment variables
            subs_dict = subs_dict.copy()
            svc_envs = {k: v for k, v in value['environment'].items() if k not in subs_dict}
            # we need to add `svc_envs` to the `subs_dict` so that it can evaluate the
            # service environment that reference to another service environment.
            subs_dict.update(svc_envs)
            svc_envs = rec_subs(svc_envs, subs_dict)
            subs_dict.update(svc_envs)

        value = {k: rec_subs(v, subs_dict) for k, v in value.items()}
    elif isinstance(value, str):

        def convert(m):
            if m.group("escaped") is not None:
                return "$"
            name = m.group("named") or m.group("braced")
            value = subs_dict.get(name)
            if value == "" and m.group("empty"):
                value = None
            if value is not None:
                return str(value)
            if m.group("err") is not None:
                raise RuntimeError(m.group("err"))
            return m.group("default") or ""

        value = var_re.sub(convert, value)
    elif hasattr(value, "__iter__"):
        value = [rec_subs(i, subs_dict) for i in value]
    return value


def norm_as_list(src):
    """
    given a dictionary {key1:value1, key2: None} or list
    return a list of ["key1=value1", "key2"]
    """
    dst: list[str]
    if src is None:
        dst = []
    elif isinstance(src, dict):
        dst = [(f"{k}={v}" if v is not None else k) for k, v in src.items()]
    elif is_list(src):
        dst = list(src)
    else:
        dst = [src]
    return dst


def norm_as_dict(src):
    """
    given a list ["key1=value1", "key2"]
    return a dictionary {key1:value1, key2: None}
    """
    if src is None:
        dst = {}
    elif isinstance(src, dict):
        dst = dict(src)
    elif is_list(src):
        dst = [i.split("=", 1) for i in src if i]
        dst = [(a if len(a) == 2 else (a[0], None)) for a in dst]
        dst = dict(dst)
    elif isinstance(src, str):
        key, value = src.split("=", 1) if "=" in src else (src, None)
        dst = {key: value}
    else:
        raise ValueError("dictionary or iterable is expected")
    return dst


def norm_ulimit(inner_value):
    if isinstance(inner_value, dict):
        if not inner_value.keys() & {"soft", "hard"}:
            raise ValueError("expected at least one soft or hard limit")
        soft = inner_value.get("soft", inner_value.get("hard"))
        hard = inner_value.get("hard", inner_value.get("soft"))
        return f"{soft}:{hard}"
    if is_list(inner_value):
        return norm_ulimit(norm_as_dict(inner_value))
    # if int or string return as is
    return inner_value


def default_network_name_for_project(compose, net, is_ext):
    if is_ext:
        return net

    default_net_name_compat = compose.x_podman.get("default_net_name_compat", False)
    if default_net_name_compat is True:
        return f"{compose.project_name.replace('-', '')}_{net}"
    return f"{compose.project_name}_{net}"


# def tr_identity(project_name, given_containers):
#    pod_name = f'pod_{project_name}'
#    pod = dict(name=pod_name)
#    containers = []
#    for cnt in given_containers:
#        containers.append(dict(cnt, pod=pod_name))
#    return [pod], containers


def transform(args, project_name, given_containers):
    if not args.in_pod_bool:
        pod_name = None
        pods = []
    else:
        pod_name = f"pod_{project_name}"
        pod = {"name": pod_name}
        pods = [pod]
    containers = []
    for cnt in given_containers:
        containers.append(dict(cnt, pod=pod_name))
    return pods, containers


async def assert_volume(compose, mount_dict):
    """
    inspect volume to get directory
    create volume if needed
    """
    vol = mount_dict.get("_vol")
    if mount_dict["type"] == "bind":
        basedir = os.path.realpath(compose.dirname)
        mount_src = mount_dict["source"]
        mount_src = os.path.realpath(os.path.join(basedir, os.path.expanduser(mount_src)))
        if not os.path.exists(mount_src):
            try:
                os.makedirs(mount_src, exist_ok=True)
            except OSError:
                pass
        return
    if mount_dict["type"] != "volume" or not vol or not vol.get("name"):
        return
    vol_name = vol["name"]
    is_ext = vol.get("external")
    log.debug("podman volume inspect %s || podman volume create %s", vol_name, vol_name)
    # TODO: might move to using "volume list"
    # podman volume list --format '{{.Name}}\t{{.MountPoint}}' \
    #     -f 'label=io.podman.compose.project=HERE'
    try:
        _ = (await compose.podman.output([], "volume", ["inspect", vol_name])).decode("utf-8")
    except subprocess.CalledProcessError as e:
        if is_ext:
            raise RuntimeError(f"External volume [{vol_name}] does not exists") from e
        labels = vol.get("labels", [])
        args = [
            "create",
            "--label",
            f"io.podman.compose.project={compose.project_name}",
            "--label",
            f"com.docker.compose.project={compose.project_name}",
        ]
        for item in norm_as_list(labels):
            args.extend(["--label", item])
        driver = vol.get("driver")
        if driver:
            args.extend(["--driver", driver])
        driver_opts = vol.get("driver_opts", {})
        for opt, value in driver_opts.items():
            args.extend(["--opt", f"{opt}={value}"])
        args.append(vol_name)
        await compose.podman.output([], "volume", args)
        _ = (await compose.podman.output([], "volume", ["inspect", vol_name])).decode("utf-8")


def mount_desc_to_mount_args(compose, mount_desc, srv_name, cnt_name):  # pylint: disable=unused-argument
    mount_type = mount_desc.get("type")
    vol = mount_desc.get("_vol") if mount_type == "volume" else None
    source = vol["name"] if vol else mount_desc.get("source")
    target = mount_desc["target"]
    opts = []
    if mount_desc.get(mount_type, None):
        # TODO: we might need to add mount_dict[mount_type]["propagation"] = "z"
        mount_prop = mount_desc.get(mount_type, {}).get("propagation")
        if mount_prop:
            opts.append(f"{mount_type}-propagation={mount_prop}")
    if mount_desc.get("read_only", False):
        opts.append("ro")
    if mount_type == "tmpfs":
        tmpfs_opts = mount_desc.get("tmpfs", {})
        tmpfs_size = tmpfs_opts.get("size")
        if tmpfs_size:
            opts.append(f"tmpfs-size={tmpfs_size}")
        tmpfs_mode = tmpfs_opts.get("mode")
        if tmpfs_mode:
            opts.append(f"tmpfs-mode={tmpfs_mode}")
    if mount_type == "bind":
        bind_opts = mount_desc.get("bind", {})
        selinux = bind_opts.get("selinux")
        if selinux is not None:
            opts.append(selinux)
    opts = ",".join(opts)
    if mount_type == "bind":
        return f"type=bind,source={source},destination={target},{opts}".rstrip(",")
    if mount_type == "volume":
        return f"type=volume,source={source},destination={target},{opts}".rstrip(",")
    if mount_type == "tmpfs":
        return f"type=tmpfs,destination={target},{opts}".rstrip(",")
    raise ValueError("unknown mount type:" + mount_type)


def ulimit_to_ulimit_args(ulimit, podman_args):
    if ulimit is not None:
        # ulimit can be a single value, i.e. ulimit: host
        if isinstance(ulimit, str):
            podman_args.extend(["--ulimit", ulimit])
        # or a dictionary or list:
        else:
            ulimit = norm_as_dict(ulimit)
            ulimit = [
                "{}={}".format(ulimit_key, norm_ulimit(inner_value))
                for ulimit_key, inner_value in ulimit.items()
            ]
            for i in ulimit:
                podman_args.extend(["--ulimit", i])


def container_to_ulimit_args(cnt, podman_args):
    ulimit_to_ulimit_args(cnt.get("ulimits", []), podman_args)


def container_to_ulimit_build_args(cnt, podman_args):
    build = cnt.get("build")

    if build is not None:
        ulimit_to_ulimit_args(build.get("ulimits", []), podman_args)


def mount_desc_to_volume_args(compose, mount_desc, srv_name, cnt_name):  # pylint: disable=unused-argument
    mount_type = mount_desc["type"]
    if mount_type not in ("bind", "volume"):
        raise ValueError("unknown mount type:" + mount_type)
    vol = mount_desc.get("_vol") if mount_type == "volume" else None
    source = vol["name"] if vol else mount_desc.get("source")
    if not source:
        raise ValueError(f"missing mount source for {mount_type} on {srv_name}")
    target = mount_desc["target"]
    opts = []

    propagations = set(filteri(mount_desc.get(mount_type, {}).get("propagation", "").split(",")))
    if mount_type != "bind":
        propagations.update(filteri(mount_desc.get("bind", {}).get("propagation", "").split(",")))
    opts.extend(propagations)
    # --volume, -v[=[[SOURCE-VOLUME|HOST-DIR:]CONTAINER-DIR[:OPTIONS]]]
    # [rw|ro]
    # [z|Z]
    # [[r]shared|[r]slave|[r]private]|[r]unbindable
    # [[r]bind]
    # [noexec|exec]
    # [nodev|dev]
    # [nosuid|suid]
    # [O]
    # [U]
    read_only = mount_desc.get("read_only")
    if read_only is not None:
        opts.append("ro" if read_only else "rw")
    if mount_type == "bind":
        bind_opts = mount_desc.get("bind", {})
        selinux = bind_opts.get("selinux")
        if selinux is not None:
            opts.append(selinux)

    args = f"{source}:{target}"
    if opts:
        args += ":" + ",".join(opts)
    return args


def get_mnt_dict(compose, cnt, volume):
    srv_name = cnt["_service"]
    basedir = compose.dirname
    if isinstance(volume, str):
        volume = parse_short_mount(volume, basedir)
    return fix_mount_dict(compose, volume, srv_name)


async def get_mount_args(compose, cnt, volume):
    volume = get_mnt_dict(compose, cnt, volume)
    srv_name = cnt["_service"]
    mount_type = volume["type"]
    await assert_volume(compose, volume)
    if compose.prefer_volume_over_mount:
        if mount_type == "tmpfs":
            # TODO: --tmpfs /tmp:rw,size=787448k,mode=1777
            args = volume["target"]
            tmpfs_opts = volume.get("tmpfs", {})
            opts = []
            size = tmpfs_opts.get("size")
            if size:
                opts.append(f"size={size}")
            mode = tmpfs_opts.get("mode")
            if mode:
                opts.append(f"mode={mode}")
            if opts:
                args += ":" + ",".join(opts)
            return ["--tmpfs", args]
        args = mount_desc_to_volume_args(compose, volume, srv_name, cnt["name"])
        return ["-v", args]
    args = mount_desc_to_mount_args(compose, volume, srv_name, cnt["name"])
    return ["--mount", args]


def get_secret_args(compose, cnt, secret, podman_is_building=False):
    """
    podman_is_building: True if we are preparing arguments for an invocation of "podman build"
                        False if we are preparing for something else like "podman run"
    """
    secret_name = secret if isinstance(secret, str) else secret.get("source")
    if not secret_name or secret_name not in compose.declared_secrets.keys():
        raise ValueError(f'ERROR: undeclared secret: "{secret}", service: {cnt["_service"]}')
    declared_secret = compose.declared_secrets[secret_name]

    source_file = declared_secret.get("file")
    dest_file = ""
    secret_opts = ""

    secret_target = None
    secret_uid = None
    secret_gid = None
    secret_mode = None
    secret_type = None
    if isinstance(secret, dict):
        secret_target = secret.get("target")
        secret_uid = secret.get("uid")
        secret_gid = secret.get("gid")
        secret_mode = secret.get("mode")
        secret_type = secret.get("type")

    if source_file:
        # assemble path for source file first, because we need it for all cases
        basedir = compose.dirname
        source_file = os.path.realpath(os.path.join(basedir, os.path.expanduser(source_file)))

        if podman_is_building:
            # pass file secrets to "podman build" with param --secret
            if not secret_target:
                secret_id = secret_name
            elif "/" in secret_target:
                raise ValueError(
                    f'ERROR: Build secret "{secret_name}" has invalid target "{secret_target}". '
                    + "(Expected plain filename without directory as target.)"
                )
            else:
                secret_id = secret_target
            volume_ref = ["--secret", f"id={secret_id},src={source_file}"]
        else:
            # pass file secrets to "podman run" as volumes
            if not secret_target:
                dest_file = "/run/secrets/{}".format(secret_name)
            elif not secret_target.startswith("/"):
                sec = secret_target if secret_target else secret_name
                dest_file = f"/run/secrets/{sec}"
            else:
                dest_file = secret_target
            volume_ref = ["--volume", f"{source_file}:{dest_file}:ro,rprivate,rbind"]

        if secret_uid or secret_gid or secret_mode:
            sec = secret_target if secret_target else secret_name
            log.warning(
                "WARNING: Service %s uses secret %s with uid, gid, or mode."
                + " These fields are not supported by this implementation of the Compose file",
                cnt["_service"],
                sec,
            )
        return volume_ref
    # v3.5 and up added external flag, earlier the spec
    # only required a name to be specified.
    # docker-compose does not support external secrets outside of swarm mode.
    # However accessing these via podman is trivial
    # since these commands are directly translated to
    # podman-create commands, albeit we can only support a 1:1 mapping
    # at the moment
    if declared_secret.get("external", False) or declared_secret.get("name"):
        secret_opts += f",uid={secret_uid}" if secret_uid else ""
        secret_opts += f",gid={secret_gid}" if secret_gid else ""
        secret_opts += f",mode={secret_mode}" if secret_mode else ""
        secret_opts += f",type={secret_type}" if secret_type else ""
        secret_opts += f",target={secret_target}" if secret_target and secret_type == "env" else ""
        # The target option is only valid for type=env,
        # which in an ideal world would work
        # for type=mount as well.
        # having a custom name for the external secret
        # has the same problem as well
        ext_name = declared_secret.get("name")
        err_str = (
            'ERROR: Custom name/target reference "{}" '
            'for mounted external secret "{}" is not supported'
        )
        if ext_name and ext_name != secret_name:
            raise ValueError(err_str.format(secret_name, ext_name))
        if secret_target and secret_target != secret_name and secret_type != 'env':
            raise ValueError(err_str.format(secret_target, secret_name))
        if secret_target and secret_type != 'env':
            log.warning(
                'WARNING: Service "%s" uses target: "%s" for secret: "%s".'
                + " That is un-supported and a no-op and is ignored.",
                cnt["_service"],
                secret_target,
                secret_name,
            )
        return ["--secret", "{}{}".format(secret_name, secret_opts)]

    raise ValueError(
        'ERROR: unparsable secret: "{}", service: "{}"'.format(secret_name, cnt["_service"])
    )


def container_to_res_args(cnt, podman_args):
    container_to_cpu_res_args(cnt, podman_args)
    container_to_gpu_res_args(cnt, podman_args)


def container_to_gpu_res_args(cnt, podman_args):
    # https://docs.docker.com/compose/gpu-support/
    # https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html

    deploy = cnt.get("deploy", {})
    res = deploy.get("resources", {})
    reservations = res.get("reservations", {})
    devices = reservations.get("devices", [])
    gpu_on = False
    for device in devices:
        driver = device.get("driver")
        if driver is None:
            continue

        capabilities = device.get("capabilities")
        if capabilities is None:
            continue

        if driver != "nvidia" or "gpu" not in capabilities:
            continue

        count = device.get("count", "all")
        device_ids = device.get("device_ids", "all")
        if device_ids != "all" and len(device_ids) > 0:
            for device_id in device_ids:
                podman_args.extend((
                    "--device",
                    f"nvidia.com/gpu={device_id}",
                ))
            gpu_on = True
            continue

        if count != "all":
            for device_id in range(count):
                podman_args.extend((
                    "--device",
                    f"nvidia.com/gpu={device_id}",
                ))
            gpu_on = True
            continue

        podman_args.extend((
            "--device",
            "nvidia.com/gpu=all",
        ))
        gpu_on = True

    if gpu_on:
        podman_args.append("--security-opt=label=disable")


def container_to_cpu_res_args(cnt, podman_args):
    # v2: https://docs.docker.com/compose/compose-file/compose-file-v2/#cpu-and-other-resources
    # cpus, cpu_shares, mem_limit, mem_reservation
    cpus_limit_v2 = try_float(cnt.get("cpus"), None)
    cpu_shares_v2 = try_int(cnt.get("cpu_shares"), None)
    mem_limit_v2 = cnt.get("mem_limit")
    mem_res_v2 = cnt.get("mem_reservation")
    # v3: https://docs.docker.com/compose/compose-file/compose-file-v3/#resources
    # spec: https://github.com/compose-spec/compose-spec/blob/master/deploy.md#resources
    # deploy.resources.{limits,reservations}.{cpus, memory}
    deploy = cnt.get("deploy", {})
    res = deploy.get("resources", {})
    limits = res.get("limits", {})
    cpus_limit_v3 = try_float(limits.get("cpus"), None)
    mem_limit_v3 = limits.get("memory")
    reservations = res.get("reservations", {})
    # cpus_res_v3 = try_float(reservations.get('cpus', None), None)
    mem_res_v3 = reservations.get("memory")
    # add args
    cpus = cpus_limit_v3 or cpus_limit_v2
    if cpus:
        podman_args.extend((
            "--cpus",
            str(cpus),
        ))
    if cpu_shares_v2:
        podman_args.extend((
            "--cpu-shares",
            str(cpu_shares_v2),
        ))
    mem = mem_limit_v3 or mem_limit_v2
    if mem:
        podman_args.extend((
            "-m",
            str(mem).lower(),
        ))
    mem_res = mem_res_v3 or mem_res_v2
    if mem_res:
        podman_args.extend((
            "--memory-reservation",
            str(mem_res).lower(),
        ))


def port_dict_to_str(port_desc):
    # NOTE: `mode: host|ingress` is ignored
    cnt_port = port_desc.get("target")
    published = port_desc.get("published", "")
    host_ip = port_desc.get("host_ip")
    protocol = port_desc.get("protocol", "tcp")
    if not cnt_port:
        raise ValueError("target container port must be specified")
    if host_ip:
        ret = f"{host_ip}:{published}:{cnt_port}"
    else:
        ret = f"{published}:{cnt_port}" if published else f"{cnt_port}"
    if protocol != "tcp":
        ret += f"/{protocol}"
    return ret


def norm_ports(ports_in):
    if not ports_in:
        ports_in = []
    if isinstance(ports_in, str):
        ports_in = [ports_in]
    ports_out = []
    for port in ports_in:
        if isinstance(port, dict):
            port = port_dict_to_str(port)
        elif isinstance(port, int):
            port = str(port)
        elif not isinstance(port, str):
            raise TypeError("port should be either string or dict")
        ports_out.append(port)
    return ports_out


def get_network_create_args(net_desc, proj_name, net_name):
    args = [
        "create",
        "--label",
        f"io.podman.compose.project={proj_name}",
        "--label",
        f"com.docker.compose.project={proj_name}",
    ]
    # TODO: add more options here, like dns, ipv6, etc.
    labels = net_desc.get("labels", [])
    for item in norm_as_list(labels):
        args.extend(["--label", item])
    if net_desc.get("internal"):
        args.append("--internal")
    driver = net_desc.get("driver")
    if driver:
        args.extend(("--driver", driver))
    driver_opts = net_desc.get("driver_opts", {})
    for key, value in driver_opts.items():
        args.extend(("--opt", f"{key}={value}"))
    ipam = net_desc.get("ipam", {})
    ipam_driver = ipam.get("driver")
    if ipam_driver and ipam_driver != "default":
        args.extend(("--ipam-driver", ipam_driver))
    ipam_config_ls = ipam.get("config", [])
    if net_desc.get("enable_ipv6"):
        args.append("--ipv6")

    if isinstance(ipam_config_ls, dict):
        ipam_config_ls = [ipam_config_ls]
    for ipam_config in ipam_config_ls:
        subnet = ipam_config.get("subnet")
        ip_range = ipam_config.get("ip_range")
        gateway = ipam_config.get("gateway")
        if subnet:
            args.extend(("--subnet", subnet))
        if ip_range:
            args.extend(("--ip-range", ip_range))
        if gateway:
            args.extend(("--gateway", gateway))
    args.append(net_name)

    return args


async def assert_cnt_nets(compose, cnt):
    """
    create missing networks
    """
    net = cnt.get("network_mode")
    if net:
        return

    cnt_nets = cnt.get("networks")
    if cnt_nets and isinstance(cnt_nets, dict):
        cnt_nets = list(cnt_nets.keys())
    cnt_nets = norm_as_list(cnt_nets or compose.default_net)
    for net in cnt_nets:
        net_desc = compose.networks[net] or {}
        is_ext = net_desc.get("external")
        ext_desc = is_ext if isinstance(is_ext, dict) else {}
        default_net_name = default_network_name_for_project(compose, net, is_ext)
        net_name = ext_desc.get("name") or net_desc.get("name") or default_net_name
        try:
            await compose.podman.output([], "network", ["exists", net_name])
        except subprocess.CalledProcessError as e:
            if is_ext:
                raise RuntimeError(f"External network [{net_name}] does not exists") from e
            args = get_network_create_args(net_desc, compose.project_name, net_name)
            await compose.podman.output([], "network", args)
            await compose.podman.output([], "network", ["exists", net_name])


def get_net_args_from_network_mode(compose, cnt):
    net_args = []
    net = cnt.get("network_mode")
    service_name = cnt["service_name"]

    if "networks" in cnt:
        raise ValueError(
            f"networks and network_mode must not be present in the same service [{service_name}]"
        )

    if net == "none":
        net_args.append("--network=none")
    elif net == "host":
        net_args.append(f"--network={net}")
    elif net.startswith("slirp4netns"):  # Note: podman-specific network mode
        net_args.append(f"--network={net}")
    elif net == "private":  # Note: podman-specific network mode
        net_args.append("--network=private")
    elif net.startswith("pasta"):  # Note: podman-specific network mode
        net_args.append(f"--network={net}")
    elif net.startswith("ns:"):  # Note: podman-specific network mode
        net_args.append(f"--network={net}")
    elif net.startswith("service:"):
        other_srv = net.split(":", 1)[1].strip()
        other_cnt = compose.container_names_by_service[other_srv][0]
        net_args.append(f"--network=container:{other_cnt}")
    elif net.startswith("container:"):
        other_cnt = net.split(":", 1)[1].strip()
        net_args.append(f"--network=container:{other_cnt}")
    elif net.startswith("bridge"):
        aliases_on_container = [service_name]
        if cnt.get("_aliases"):
            aliases_on_container.extend(cnt.get("_aliases"))
        net_options = [f"alias={alias}" for alias in aliases_on_container]
        mac_address = cnt.get("mac_address")
        if mac_address:
            net_options.append(f"mac={mac_address}")

        net = f"{net}," if ":" in net else f"{net}:"
        net_args.append(f"--network={net}{','.join(net_options)}")
    else:
        log.fatal("unknown network_mode [%s]", net)
        sys.exit(1)

    return net_args


def get_net_args(compose, cnt):
    net = cnt.get("network_mode")
    if net:
        return get_net_args_from_network_mode(compose, cnt)

    return get_net_args_from_networks(compose, cnt)


def get_net_args_from_networks(compose, cnt):
    net_args = []
    mac_address = cnt.get("mac_address")
    service_name = cnt["service_name"]

    aliases_on_container = [service_name]
    aliases_on_container.extend(cnt.get("_aliases", []))

    multiple_nets = cnt.get("networks", {})
    if not multiple_nets:
        if not compose.default_net:
            # The bridge mode in podman is using the `podman` network.
            # It seems weird, but we should keep this behavior to avoid
            # breaking changes.
            net_options = [f"alias={alias}" for alias in aliases_on_container]
            if mac_address:
                net_options.append(f"mac={mac_address}")
            net_args.append(f"--network=bridge:{','.join(net_options)}")
            return net_args

        multiple_nets = {compose.default_net: {}}

    # networks can be specified as a dict with config per network or as a plain list without
    # config.  Support both cases by converting the plain list to a dict with empty config.
    if is_list(multiple_nets):
        multiple_nets = {net: {} for net in multiple_nets}
    else:
        multiple_nets = {net: net_config or {} for net, net_config in multiple_nets.items()}

    # if a mac_address was specified on the container level, we need to check that it is not
    # specified on the network level as well
    if mac_address is not None:
        for net_config in multiple_nets.values():
            network_mac = net_config.get("x-podman.mac_address")
            if network_mac is not None:
                raise RuntimeError(
                    f"conflicting mac addresses {mac_address} and {network_mac}:"
                    "specifying mac_address on both container and network level "
                    "is not supported"
                )

    for net_, net_config_ in multiple_nets.items():
        net_desc = compose.networks.get(net_) or {}
        is_ext = net_desc.get("external")
        ext_desc = is_ext if isinstance(is_ext, str) else {}
        default_net_name = default_network_name_for_project(compose, net_, is_ext)
        net_name = ext_desc.get("name") or net_desc.get("name") or default_net_name

        ipv4 = net_config_.get("ipv4_address")
        ipv6 = net_config_.get("ipv6_address")
        # custom extension; not supported by docker-compose v3
        mac = net_config_.get("x-podman.mac_address")
        aliases_on_net = norm_as_list(net_config_.get("aliases", []))

        # if a mac_address was specified on the container level, apply it to the first network
        # This works for Python > 3.6, because dict insert ordering is preserved, so we are
        # sure that the first network we encounter here is also the first one specified by
        # the user
        if mac is None and mac_address is not None:
            mac = mac_address
            mac_address = None

        net_options = []
        if ipv4:
            net_options.append(f"ip={ipv4}")
        if ipv6:
            net_options.append(f"ip6={ipv6}")
        if mac:
            net_options.append(f"mac={mac}")

        # Container level service aliases
        net_options.extend([f"alias={alias}" for alias in aliases_on_container])
        # network level service aliases
        if aliases_on_net:
            net_options.extend([f"alias={alias}" for alias in aliases_on_net])

        if net_options:
            net_args.append(f"--network={net_name}:" + ",".join(net_options))
        else:
            net_args.append(f"--network={net_name}")

    return net_args


async def container_to_args(compose, cnt, detached=True):
    # TODO: double check -e , --add-host, -v, --read-only
    dirname = compose.dirname
    pod = cnt.get("pod", "")
    name = cnt["name"]
    podman_args = [f"--name={name}"]

    if detached:
        podman_args.append("-d")

    if pod:
        podman_args.append(f"--pod={pod}")
    deps = []
    for dep_srv in cnt.get("_deps", []):
        deps.extend(compose.container_names_by_service.get(dep_srv.name, []))
    if deps:
        deps_csv = ",".join(deps)
        podman_args.append(f"--requires={deps_csv}")
    sec = norm_as_list(cnt.get("security_opt"))
    for sec_item in sec:
        podman_args.extend(["--security-opt", sec_item])
    ann = norm_as_list(cnt.get("annotations"))
    for a in ann:
        podman_args.extend(["--annotation", a])
    if cnt.get("read_only"):
        podman_args.append("--read-only")
    if cnt.get("http_proxy") is False:
        podman_args.append("--http-proxy=false")
    for i in cnt.get("labels", []):
        podman_args.extend(["--label", i])
    for c in cnt.get("cap_add", []):
        podman_args.extend(["--cap-add", c])
    for c in cnt.get("cap_drop", []):
        podman_args.extend(["--cap-drop", c])
    for item in cnt.get("group_add", []):
        podman_args.extend(["--group-add", item])
    for item in cnt.get("devices", []):
        podman_args.extend(["--device", item])
    for item in cnt.get("device_cgroup_rules", []):
        podman_args.extend(["--device-cgroup-rule", item])
    for item in norm_as_list(cnt.get("dns")):
        podman_args.extend(["--dns", item])
    for item in norm_as_list(cnt.get("dns_opt")):
        podman_args.extend(["--dns-opt", item])
    for item in norm_as_list(cnt.get("dns_search")):
        podman_args.extend(["--dns-search", item])
    env_file = cnt.get("env_file", [])
    if isinstance(env_file, (dict, str)):
        env_file = [env_file]
    for i in env_file:
        if isinstance(i, str):
            i = {"path": i}
        path = i["path"]
        required = i.get("required", True)
        i = os.path.realpath(os.path.join(dirname, path))
        if not os.path.exists(i):
            if not required:
                continue
            raise ValueError("Env file at {} does not exist".format(i))
        dotenv_dict = {}
        dotenv_dict = dotenv_to_dict(i)
        env = norm_as_list(dotenv_dict)
        for e in env:
            podman_args.extend(["-e", e])
    env = norm_as_list(cnt.get("environment", {}))
    for e in env:
        podman_args.extend(["-e", e])
    tmpfs_ls = cnt.get("tmpfs", [])
    if isinstance(tmpfs_ls, str):
        tmpfs_ls = [tmpfs_ls]
    for i in tmpfs_ls:
        podman_args.extend(["--tmpfs", i])
    for volume in cnt.get("volumes", []):
        podman_args.extend(await get_mount_args(compose, cnt, volume))

    await assert_cnt_nets(compose, cnt)
    podman_args.extend(get_net_args(compose, cnt))

    log_config = cnt.get("logging")
    if log_config is not None:
        podman_args.append(f'--log-driver={log_config.get("driver", "k8s-file")}')
        log_opts = log_config.get("options", {})
        podman_args += [f"--log-opt={name}={value}" for name, value in log_opts.items()]
    for secret in cnt.get("secrets", []):
        podman_args.extend(get_secret_args(compose, cnt, secret))
    for i in cnt.get("extra_hosts", []):
        podman_args.extend(["--add-host", i])
    for i in cnt.get("expose", []):
        podman_args.extend(["--expose", i])
    if cnt.get("publishall"):
        podman_args.append("-P")
    ports = cnt.get("ports", [])
    if isinstance(ports, str):
        ports = [ports]
    for port in ports:
        if isinstance(port, dict):
            port = port_dict_to_str(port)
        elif not isinstance(port, str):
            raise TypeError("port should be either string or dict")
        podman_args.extend(["-p", port])

    userns_mode = cnt.get("userns_mode")
    if userns_mode is not None:
        podman_args.extend(["--userns", userns_mode])

    user = cnt.get("user")
    if user is not None:
        podman_args.extend(["-u", user])
    if cnt.get("working_dir") is not None:
        podman_args.extend(["-w", cnt["working_dir"]])
    if cnt.get("hostname"):
        podman_args.extend(["--hostname", cnt["hostname"]])
    if cnt.get("shm_size"):
        podman_args.extend(["--shm-size", str(cnt["shm_size"])])
    if cnt.get("stdin_open"):
        podman_args.append("-i")
    if cnt.get("stop_signal"):
        podman_args.extend(["--stop-signal", cnt["stop_signal"]])

    sysctls = cnt.get("sysctls")
    if sysctls is not None:
        if isinstance(sysctls, dict):
            for sysctl, value in sysctls.items():
                podman_args.extend(["--sysctl", "{}={}".format(sysctl, value)])
        elif isinstance(sysctls, list):
            for i in sysctls:
                podman_args.extend(["--sysctl", i])
        else:
            raise TypeError("sysctls should be either dict or list")

    if cnt.get("tty"):
        podman_args.append("--tty")
    if cnt.get("privileged"):
        podman_args.append("--privileged")
    if cnt.get("pid"):
        podman_args.extend(["--pid", cnt["pid"]])
    pull_policy = cnt.get("pull_policy")
    if pull_policy is not None and pull_policy != "build":
        podman_args.extend(["--pull", pull_policy])
    if cnt.get("restart") is not None:
        podman_args.extend(["--restart", cnt["restart"]])
    container_to_ulimit_args(cnt, podman_args)
    container_to_res_args(cnt, podman_args)
    # currently podman shipped by fedora does not package this
    if cnt.get("init"):
        podman_args.append("--init")
    if cnt.get("init-path"):
        podman_args.extend(["--init-path", cnt["init-path"]])
    entrypoint = cnt.get("entrypoint")
    if entrypoint is not None:
        if isinstance(entrypoint, str):
            entrypoint = shlex.split(entrypoint)
        podman_args.extend(["--entrypoint", json.dumps(entrypoint)])
    platform = cnt.get("platform")
    if platform is not None:
        podman_args.extend(["--platform", platform])
    if cnt.get("runtime"):
        podman_args.extend(["--runtime", cnt["runtime"]])

    # WIP: healthchecks are still work in progress
    healthcheck = cnt.get("healthcheck", {})
    if not isinstance(healthcheck, dict):
        raise ValueError("'healthcheck' must be a key-value mapping")
    healthcheck_disable = healthcheck.get("disable", False)
    healthcheck_test = healthcheck.get("test")
    if healthcheck_disable:
        healthcheck_test = ["NONE"]
    if healthcheck_test:
        # If it's a string, it's equivalent to specifying CMD-SHELL
        if isinstance(healthcheck_test, str):
            # podman does not add shell to handle command with whitespace
            podman_args.extend([
                "--healthcheck-command",
                "/bin/sh -c " + cmd_quote(healthcheck_test),
            ])
        elif is_list(healthcheck_test):
            healthcheck_test = healthcheck_test.copy()
            # If it's a list, first item is either NONE, CMD or CMD-SHELL.
            healthcheck_type = healthcheck_test.pop(0)
            if healthcheck_type == "NONE":
                podman_args.append("--no-healthcheck")
            elif healthcheck_type == "CMD":
                cmd_q = "' '".join([cmd_quote(i) for i in healthcheck_test])
                podman_args.extend(["--healthcheck-command", "/bin/sh -c " + cmd_q])
            elif healthcheck_type == "CMD-SHELL":
                if len(healthcheck_test) != 1:
                    raise ValueError("'CMD_SHELL' takes a single string after it")
                cmd_q = cmd_quote(healthcheck_test[0])
                podman_args.extend(["--healthcheck-command", "/bin/sh -c " + cmd_q])
            else:
                raise ValueError(
                    f"unknown healthcheck test type [{healthcheck_type}],\
                     expecting NONE, CMD or CMD-SHELL."
                )
        else:
            raise ValueError("'healthcheck.test' either a string or a list")

    # interval, timeout and start_period are specified as durations.
    if "interval" in healthcheck:
        podman_args.extend(["--healthcheck-interval", healthcheck["interval"]])
    if "timeout" in healthcheck:
        podman_args.extend(["--healthcheck-timeout", healthcheck["timeout"]])
    if "start_period" in healthcheck:
        podman_args.extend(["--healthcheck-start-period", healthcheck["start_period"]])

    # convert other parameters to string
    if "retries" in healthcheck:
        podman_args.extend(["--healthcheck-retries", str(healthcheck["retries"])])

    # handle podman extension
    if 'x-podman' in cnt:
        raise ValueError(
            'Configuration under x-podman has been migrated to x-podman.uidmaps and '
            'x-podman.gidmaps fields'
        )

    rootfs_mode = False
    for uidmap in cnt.get('x-podman.uidmaps', []):
        podman_args.extend(["--uidmap", uidmap])
    for gidmap in cnt.get('x-podman.gidmaps', []):
        podman_args.extend(["--gidmap", gidmap])
    if cnt.get("x-podman.no_hosts", False):
        podman_args.extend(["--no-hosts"])
    rootfs = cnt.get('x-podman.rootfs')
    if rootfs is not None:
        rootfs_mode = True
        podman_args.extend(["--rootfs", rootfs])
        log.warning("WARNING: x-podman.rootfs and image both specified, image field ignored")

    if not rootfs_mode:
        podman_args.append(cnt["image"])  # command, ..etc.
    command = cnt.get("command")
    if command is not None:
        if isinstance(command, str):
            podman_args.extend(shlex.split(command))
        else:
            podman_args.extend([str(i) for i in command])
    return podman_args


class ServiceDependencyCondition(Enum):
    CONFIGURED = "configured"
    CREATED = "created"
    EXITED = "exited"
    HEALTHY = "healthy"
    INITIALIZED = "initialized"
    PAUSED = "paused"
    REMOVING = "removing"
    RUNNING = "running"
    STOPPED = "stopped"
    STOPPING = "stopping"
    UNHEALTHY = "unhealthy"

    @classmethod
    def from_value(cls, value):
        # Check if the value exists in the enum
        for member in cls:
            if member.value == value:
                return member

        # Check if this is a value coming from  reference
        docker_to_podman_cond = {
            "service_healthy": ServiceDependencyCondition.HEALTHY,
            "service_started": ServiceDependencyCondition.RUNNING,
            "service_completed_successfully": ServiceDependencyCondition.STOPPED,
        }
        try:
            return docker_to_podman_cond[value]
        except KeyError:
            raise ValueError(f"Value '{value}' is not a valid condition for a service dependency")  # pylint: disable=raise-missing-from


class ServiceDependency:
    def __init__(self, name, condition):
        self._name = name
        self._condition = ServiceDependencyCondition.from_value(condition)

    @property
    def name(self):
        return self._name

    @property
    def condition(self):
        return self._condition

    def __hash__(self):
        # Compute hash based on the frozenset of items to ensure order does not matter
        return hash(('name', self._name) + ('condition', self._condition))

    def __eq__(self, other):
        # Compare equality based on dictionary content
        if isinstance(other, ServiceDependency):
            return self._name == other.name and self._condition == other.condition
        return False


def rec_deps(services, service_name, start_point=None):
    """
    return all dependencies of service_name recursively
    """
    if not start_point:
        start_point = service_name
    deps = services[service_name]["_deps"]
    for dep_name in deps.copy():
        # avoid A depens on A
        if dep_name.name == service_name:
            continue
        dep_srv = services.get(dep_name.name)
        if not dep_srv:
            continue
        # NOTE: avoid creating loops, A->B->A
        if any(start_point == x.name for x in dep_srv["_deps"]):
            continue
        new_deps = rec_deps(services, dep_name.name, start_point)
        deps.update(new_deps)
    return deps


def flat_deps(services, with_extends=False):
    """
    create dependencies "_deps" or update it recursively for all services
    """
    for name, srv in services.items():
        # parse dependencies for each service
        deps = set()
        srv["_deps"] = deps
        # TODO: manage properly the dependencies coming from base services when extended
        if with_extends:
            ext = srv.get("extends", {}).get("service")
            if ext:
                if ext != name:
                    deps.add(ServiceDependency(ext, "service_started"))
                continue

        # the compose file has been normalized. depends_on, if exists, can only be a dictionary
        # the normalization adds a "service_started" condition by default
        deps_ls = srv.get("depends_on", {})
        deps_ls = [ServiceDependency(k, v["condition"]) for k, v in deps_ls.items()]
        deps.update(deps_ls)
        # parse link to get service name and remove alias
        links_ls = srv.get("links", [])
        if not is_list(links_ls):
            links_ls = [links_ls]
        deps.update([ServiceDependency(c.split(":")[0], "service_started") for c in links_ls])
        for c in links_ls:
            if ":" in c:
                dep_name, dep_alias = c.split(":")
                if "_aliases" not in services[dep_name]:
                    services[dep_name]["_aliases"] = set()
                services[dep_name]["_aliases"].add(dep_alias)

    # expand the dependencies on each service
    for name, srv in services.items():
        rec_deps(services, name)


async def wait_with_timeout(coro, timeout):
    """
    Asynchronously waits for the given coroutine to complete with a timeout.

    Args:
        coro: The coroutine to wait for.
        timeout (int or float): The maximum number of seconds to wait for.

    Raises:
        TimeoutError: If the coroutine does not complete within the specified timeout.
    """
    try:
        return await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError as exc:
        raise TimeoutError from exc


###################
# podman and compose classes
###################


class Podman:
    def __init__(
        self,
        compose,
        podman_path="podman",
        dry_run=False,
        semaphore: asyncio.Semaphore = asyncio.Semaphore(sys.maxsize),
    ):
        self.compose = compose
        self.podman_path = podman_path
        self.dry_run = dry_run
        self.semaphore = semaphore

    async def output(self, podman_args, cmd="", cmd_args=None):
        async with self.semaphore:
            cmd_args = cmd_args or []
            xargs = self.compose.get_podman_args(cmd) if cmd else []
            cmd_ls = [self.podman_path, *podman_args, cmd] + xargs + cmd_args
            log.info(str(cmd_ls))
            p = await asyncio.create_subprocess_exec(
                *cmd_ls, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            stdout_data, stderr_data = await p.communicate()
            if p.returncode == 0:
                return stdout_data

            raise subprocess.CalledProcessError(p.returncode, " ".join(cmd_ls), stderr_data)

    def exec(
        self,
        podman_args,
        cmd="",
        cmd_args=None,
    ):
        cmd_args = list(map(str, cmd_args or []))
        xargs = self.compose.get_podman_args(cmd) if cmd else []
        cmd_ls = [self.podman_path, *podman_args, cmd] + xargs + cmd_args
        log.info(" ".join([str(i) for i in cmd_ls]))
        os.execlp(self.podman_path, *cmd_ls)

    async def run(  # pylint: disable=dangerous-default-value
        self,
        podman_args,
        cmd="",
        cmd_args=None,
        log_formatter=None,
        *,
        # Intentionally mutable default argument to hold references to tasks
        task_reference=set(),
    ) -> int:
        async with self.semaphore:
            cmd_args = list(map(str, cmd_args or []))
            xargs = self.compose.get_podman_args(cmd) if cmd else []
            cmd_ls = [self.podman_path, *podman_args, cmd] + xargs + cmd_args
            log.info(" ".join([str(i) for i in cmd_ls]))
            if self.dry_run:
                return None

            if log_formatter is not None:

                async def format_out(stdout):
                    while True:
                        line = await stdout.readline()
                        if line:
                            print(log_formatter, line.decode('utf-8'), end='')
                        if stdout.at_eof():
                            break

                p = await asyncio.create_subprocess_exec(
                    *cmd_ls, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )  # pylint: disable=consider-using-with

                # This is hacky to make the tasks not get garbage collected
                # https://github.com/python/cpython/issues/91887
                out_t = asyncio.create_task(format_out(p.stdout))
                task_reference.add(out_t)
                out_t.add_done_callback(task_reference.discard)

                err_t = asyncio.create_task(format_out(p.stderr))
                task_reference.add(err_t)
                err_t.add_done_callback(task_reference.discard)

            else:
                p = await asyncio.create_subprocess_exec(*cmd_ls)  # pylint: disable=consider-using-with

            try:
                exit_code = await p.wait()
            except asyncio.CancelledError:
                log.info("Sending termination signal")
                p.terminate()
                try:
                    exit_code = await wait_with_timeout(p.wait(), 10)
                except TimeoutError:
                    log.warning("container did not shut down after 10 seconds, killing")
                    p.kill()
                    exit_code = await p.wait()

            log.info("exit code: %s", exit_code)
            return exit_code

    async def network_ls(self):
        output = (
            await self.output(
                [],
                "network",
                [
                    "ls",
                    "--noheading",
                    "--filter",
                    f"label=io.podman.compose.project={self.compose.project_name}",
                    "--format",
                    "{{.Name}}",
                ],
            )
        ).decode()
        networks = output.splitlines()
        return networks

    async def volume_ls(self):
        output = (
            await self.output(
                [],
                "volume",
                [
                    "ls",
                    "--noheading",
                    "--filter",
                    f"label=io.podman.compose.project={self.compose.project_name}",
                    "--format",
                    "{{.Name}}",
                ],
            )
        ).decode("utf-8")
        volumes = output.splitlines()
        return volumes


def normalize_service(service, sub_dir=""):
    if "build" in service:
        build = service["build"]
        if isinstance(build, str):
            service["build"] = {"context": build}
    if sub_dir and "build" in service:
        build = service["build"]
        context = build.get("context", "")
        if context or sub_dir:
            if context.startswith("./"):
                context = context[2:]
            if sub_dir:
                context = os.path.join(sub_dir, context)
            context = context.rstrip("/")
            if not context:
                context = "."
            service["build"]["context"] = context
    if "build" in service and "additional_contexts" in service["build"]:
        if isinstance(build["additional_contexts"], dict):
            new_additional_contexts = []
            for k, v in build["additional_contexts"].items():
                new_additional_contexts.append(f"{k}={v}")
            build["additional_contexts"] = new_additional_contexts
    for key in ("command", "entrypoint"):
        if key in service:
            if isinstance(service[key], str):
                service[key] = shlex.split(service[key])
    for key in ("env_file", "security_opt", "volumes"):
        if key not in service:
            continue
        if isinstance(service[key], str):
            service[key] = [service[key]]
    if "security_opt" in service:
        sec_ls = service["security_opt"]
        for ix, item in enumerate(sec_ls):
            if item in ("seccomp:unconfined", "apparmor:unconfined"):
                sec_ls[ix] = item.replace(":", "=")
    for key in ("environment", "labels"):
        if key not in service:
            continue
        service[key] = norm_as_dict(service[key])
    if "extends" in service:
        extends = service["extends"]
        if isinstance(extends, str):
            extends = {"service": extends}
            service["extends"] = extends
    if "depends_on" in service:
        # deps should become a dictionary of dependencies
        deps = service["depends_on"]
        if isinstance(deps, str):
            deps = {deps: {}}
        elif is_list(deps):
            deps = {x: {} for x in deps}

        # the dependency service_started is set by default
        # unless requested otherwise.
        for k, v in deps.items():
            v.setdefault('condition', 'service_started')
        service["depends_on"] = deps
    return service


def normalize(compose):
    """
    convert compose dict of some keys from string or dicts into arrays
    """
    services = compose.get("services", {})
    for service in services.values():
        normalize_service(service)
    return compose


def normalize_service_final(service: dict, project_dir: str) -> dict:
    if "build" in service:
        build = service["build"]
        context = build if isinstance(build, str) else build.get("context", ".")
        context = os.path.normpath(os.path.join(project_dir, context))
        if not isinstance(service["build"], dict):
            service["build"] = {}
        service["build"]["context"] = context
    return service


def normalize_final(compose: dict, project_dir: str) -> dict:
    services = compose.get("services", {})
    for service in services.values():
        normalize_service_final(service, project_dir)
    return compose


def clone(value):
    return value.copy() if is_list(value) or isinstance(value, dict) else value


def rec_merge_one(target, source):
    """
    update target from source recursively
    """
    done = set()
    for key, value in source.items():
        if key in target:
            continue
        target[key] = clone(value)
        done.add(key)
    for key, value in target.items():
        if key in done:
            continue
        if key not in source:
            continue
        value2 = source[key]
        if key in ("command", "entrypoint"):
            target[key] = clone(value2)
            continue
        if not isinstance(value2, type(value)):
            value_type = type(value)
            value2_type = type(value2)
            raise ValueError(f"can't merge value of [{key}] of type {value_type} and {value2_type}")
        if is_list(value2):
            if key == "volumes":
                # clean duplicate mount targets
                pts = {v.split(":", 2)[1] for v in value2 if ":" in v}
                del_ls = [
                    ix for (ix, v) in enumerate(value) if ":" in v and v.split(":", 2)[1] in pts
                ]
                for ix in reversed(del_ls):
                    del value[ix]
                value.extend(value2)
            else:
                value.extend(value2)
        elif isinstance(value2, dict):
            rec_merge_one(value, value2)
        else:
            target[key] = value2
    return target


def rec_merge(target, *sources):
    """
    update target recursively from sources
    """
    for source in sources:
        ret = rec_merge_one(target, source)
    return ret


def resolve_extends(services, service_names, environ):
    for name in service_names:
        service = services[name]
        ext = service.get("extends", {})
        if isinstance(ext, str):
            ext = {"service": ext}
        from_service_name = ext.get("service")
        if not from_service_name:
            continue
        filename = ext.get("file")
        if filename:
            if filename.startswith("./"):
                filename = filename[2:]
            with open(filename, "r", encoding="utf-8") as f:
                content = yaml.safe_load(f) or {}
            if "services" in content:
                content = content["services"]
            subdirectory = os.path.dirname(filename)
            content = rec_subs(content, environ)
            from_service = content.get(from_service_name, {}) or {}
            normalize_service(from_service, subdirectory)
        else:
            from_service = services.get(from_service_name, {}).copy()
            del from_service["_deps"]
            try:
                del from_service["extends"]
            except KeyError:
                pass
        new_service = rec_merge({}, from_service, service)
        services[name] = new_service


def dotenv_to_dict(dotenv_path):
    if not os.path.isfile(dotenv_path):
        return {}
    return dotenv_values(dotenv_path)


COMPOSE_DEFAULT_LS = [
    "compose.yaml",
    "compose.yml",
    "compose.override.yaml",
    "compose.override.yml",
    "podman-compose.yaml",
    "podman-compose.yml",
    "docker-compose.yml",
    "docker-compose.yaml",
    "docker-compose.override.yml",
    "docker-compose.override.yaml",
    "container-compose.yml",
    "container-compose.yaml",
    "container-compose.override.yml",
    "container-compose.override.yaml",
]


class PodmanCompose:
    def __init__(self):
        self.podman: Podman
        self.podman_version = None
        self.environ = {}
        self.exit_code = None
        self.commands = {}
        self.global_args = argparse.Namespace()
        self.project_name = None
        self.dirname = None
        self.pods = None
        self.containers = []
        self.vols = None
        self.networks = {}
        self.default_net = "default"
        self.declared_secrets = None
        self.container_names_by_service = None
        self.container_by_name = None
        self.services = None
        self.all_services = set()
        self.prefer_volume_over_mount = True
        self.x_podman = {}
        self.merged_yaml = None
        self.yaml_hash = ""
        self.console_colors = [
            "\x1b[1;32m",
            "\x1b[1;33m",
            "\x1b[1;34m",
            "\x1b[1;35m",
            "\x1b[1;36m",
        ]

    def assert_services(self, services):
        if isinstance(services, str):
            services = [services]
        given = set(services or [])
        missing = given - self.all_services
        if missing:
            missing_csv = ",".join(missing)
            log.warning("missing services [%s]", missing_csv)
            sys.exit(1)

    def get_podman_args(self, cmd):
        xargs = []
        for args in self.global_args.podman_args:
            xargs.extend(shlex.split(args))
        cmd_norm = cmd if cmd != "create" else "run"
        cmd_args = self.global_args.__dict__.get(f"podman_{cmd_norm}_args", [])
        for args in cmd_args:
            xargs.extend(shlex.split(args))
        return xargs

    async def run(self, argv=None):
        log.info("podman-compose version: %s", __version__)
        args = self._parse_args(argv)
        podman_path = args.podman_path
        if podman_path != "podman":
            if os.path.isfile(podman_path) and os.access(podman_path, os.X_OK):
                podman_path = os.path.realpath(podman_path)
            else:
                # this also works if podman hasn't been installed now
                if args.dry_run is False:
                    log.fatal("Binary %s has not been found.", podman_path)
                    sys.exit(1)
        self.podman = Podman(self, podman_path, args.dry_run, asyncio.Semaphore(args.parallel))

        if not args.dry_run:
            # just to make sure podman is running
            try:
                self.podman_version = (await self.podman.output(["--version"], "", [])).decode(
                    "utf-8"
                ).strip() or ""
                self.podman_version = (self.podman_version.split() or [""])[-1]
            except subprocess.CalledProcessError:
                self.podman_version = None
            if not self.podman_version:
                log.fatal("it seems that you do not have `podman` installed")
                sys.exit(1)
            log.info("using podman version: %s", self.podman_version)
        cmd_name = args.command
        compose_required = cmd_name != "version" and (
            cmd_name != "systemd" or args.action != "create-unit"
        )
        if compose_required:
            self._parse_compose_file()
        cmd = self.commands[cmd_name]
        retcode = await cmd(self, args)
        if isinstance(retcode, int):
            sys.exit(retcode)

    def resolve_in_pod(self):
        if self.global_args.in_pod_bool is None:
            self.global_args.in_pod_bool = self.x_podman.get("in_pod", True)
        # otherwise use `in_pod` value provided by command line
        return self.global_args.in_pod_bool

    def _parse_compose_file(self):
        args = self.global_args
        # cmd = args.command
        dirname = os.environ.get("COMPOSE_PROJECT_DIR")
        if dirname and os.path.isdir(dirname):
            os.chdir(dirname)
        pathsep = os.environ.get("COMPOSE_PATH_SEPARATOR", os.pathsep)
        if not args.file:
            default_str = os.environ.get("COMPOSE_FILE")
            if default_str:
                default_ls = default_str.split(pathsep)
            else:
                default_ls = COMPOSE_DEFAULT_LS
            args.file = list(filter(os.path.exists, default_ls))
        files = args.file
        if not files:
            log.fatal(
                "no compose.yaml, docker-compose.yml or container-compose.yml file found, "
                "pass files with -f"
            )
            sys.exit(-1)
        ex = map(lambda x: x == '-' or os.path.exists(x), files)
        missing = [fn0 for ex0, fn0 in zip(ex, files) if not ex0]
        if missing:
            log.fatal("missing files: %s", missing)
            sys.exit(1)
        # make absolute
        relative_files = files
        filename = files[0]
        project_name = args.project_name
        # no_ansi = args.no_ansi
        # no_cleanup = args.no_cleanup
        # dry_run = args.dry_run
        # host_env = None
        dirname = os.path.realpath(os.path.dirname(filename))
        dir_basename = os.path.basename(dirname)
        self.dirname = dirname

        # env-file is relative to the CWD
        dotenv_dict = {}
        if args.env_file:
            # Load .env from the Compose file's directory to preserve
            # behavior prior to 1.1.0 and to match with Docker Compose (v2).
            if ".env" == args.env_file:
                project_dotenv_file = os.path.realpath(os.path.join(dirname, ".env"))
                if os.path.exists(project_dotenv_file):
                    dotenv_dict.update(dotenv_to_dict(project_dotenv_file))
            dotenv_path = os.path.realpath(args.env_file)
            dotenv_dict.update(dotenv_to_dict(dotenv_path))

        # TODO: remove next line
        os.chdir(dirname)

        os.environ.update({
            key: value for key, value in dotenv_dict.items() if key.startswith("PODMAN_")
        })
        self.environ = dotenv_dict
        self.environ.update(dict(os.environ))
        # see: https://docs.docker.com/compose/reference/envvars/
        # see: https://docs.docker.com/compose/env-file/
        self.environ.update({
            "COMPOSE_PROJECT_DIR": dirname,
            "COMPOSE_FILE": pathsep.join(relative_files),
            "COMPOSE_PATH_SEPARATOR": pathsep,
        })

        if args and 'env' in args and args.env:
            env_vars = norm_as_dict(args.env)
            self.environ.update(env_vars)

        compose = {}
        # Iterate over files primitively to allow appending to files in-loop
        files_iter = iter(files)

        while True:
            try:
                filename = next(files_iter)
            except StopIteration:
                break

            if filename.strip().split('/')[-1] == '-':
                content = yaml.safe_load(sys.stdin)
            else:
                with open(filename, "r", encoding="utf-8") as f:
                    content = yaml.safe_load(f)
                # log(filename, json.dumps(content, indent = 2))
            if not isinstance(content, dict):
                sys.stderr.write(
                    "Compose file does not contain a top level object: %s\n" % filename
                )
                sys.exit(1)
            content = normalize(content)
            # log(filename, json.dumps(content, indent = 2))
            content = rec_subs(content, self.environ)
            rec_merge(compose, content)
            # If `include` is used, append included files to files
            include = compose.get("include")
            if include:
                files.extend(include)
                # As compose obj is updated and tested with every loop, not deleting `include`
                # from it, results in it being tested again and again, original values for
                # `include` be appended to `files`, and, included files be processed for ever.
                # Solution is to remove 'include' key from compose obj. This doesn't break
                # having `include` present and correctly processed in included files
                del compose["include"]
        resolved_services = self._resolve_profiles(compose.get("services", {}), set(args.profile))
        compose["services"] = resolved_services
        if not getattr(args, "no_normalize", None):
            compose = normalize_final(compose, self.dirname)
        self.merged_yaml = yaml.safe_dump(compose)
        merged_json_b = json.dumps(compose, separators=(",", ":")).encode("utf-8")
        self.yaml_hash = hashlib.sha256(merged_json_b).hexdigest()
        compose["_dirname"] = dirname
        # debug mode
        if len(files) > 1:
            log.debug(" ** merged:\n%s", json.dumps(compose, indent=2))
        # ver = compose.get('version')

        if not project_name:
            project_name = compose.get("name")
            if project_name is None:
                # More strict then actually needed for simplicity:
                # podman requires [a-zA-Z0-9][a-zA-Z0-9_.-]*
                project_name = self.environ.get("COMPOSE_PROJECT_NAME", dir_basename.lower())
                project_name = norm_re.sub("", project_name)
                if not project_name:
                    raise RuntimeError(f"Project name [{dir_basename}] normalized to empty")

        self.project_name = project_name
        self.environ.update({"COMPOSE_PROJECT_NAME": self.project_name})

        services = compose.get("services")
        if services is None:
            services = {}
            log.warning("WARNING: No services defined")
        # include services with no profile defined or the selected profiles
        services = self._resolve_profiles(services, set(args.profile))

        # NOTE: maybe add "extends.service" to _deps at this stage
        flat_deps(services, with_extends=True)
        service_names = sorted([(len(srv["_deps"]), name) for name, srv in services.items()])
        service_names = [name for _, name in service_names]
        resolve_extends(services, service_names, self.environ)
        flat_deps(services)
        service_names = sorted([(len(srv["_deps"]), name) for name, srv in services.items()])
        service_names = [name for _, name in service_names]
        nets = compose.get("networks", {})
        if not nets:
            nets["default"] = None

        self.networks = nets
        if compose.get("x-podman", {}).get("default_net_behavior_compat", False):
            # If there is no network_mode and networks in service,
            # docker-compose will create default network named '<project_name>_default'
            # and add the service to the default network.
            # So we always set `default_net = 'default'` for compatibility
            if "default" not in self.networks:
                self.networks["default"] = None
        else:
            if len(self.networks) == 1:
                self.default_net = list(nets.keys())[0]
            elif "default" in nets:
                self.default_net = "default"
            else:
                self.default_net = None

        allnets = set()
        for name, srv in services.items():
            srv_nets = srv.get("networks", self.default_net)
            srv_nets = (
                list(srv_nets.keys()) if isinstance(srv_nets, dict) else norm_as_list(srv_nets)
            )
            allnets.update(srv_nets)
        given_nets = set(nets.keys())
        missing_nets = allnets - given_nets
        unused_nets = given_nets - allnets - set(["default"])
        if len(unused_nets):
            unused_nets_str = ",".join(unused_nets)
            log.warning("WARNING: unused networks: %s", unused_nets_str)
        if len(missing_nets):
            missing_nets_str = ",".join(missing_nets)
            raise RuntimeError(f"missing networks: {missing_nets_str}")
        # volumes: [...]
        self.vols = compose.get("volumes", {})
        podman_compose_labels = [
            "io.podman.compose.config-hash=" + self.yaml_hash,
            "io.podman.compose.project=" + project_name,
            "io.podman.compose.version=" + __version__,
            f"PODMAN_SYSTEMD_UNIT=podman-compose@{project_name}.service",
            "com.docker.compose.project=" + project_name,
            "com.docker.compose.project.working_dir=" + dirname,
            "com.docker.compose.project.config_files=" + ",".join(relative_files),
        ]
        # other top-levels:
        # networks: {driver: ...}
        # configs: {...}
        self.declared_secrets = compose.get("secrets", {})
        given_containers = []
        container_names_by_service = {}
        self.services = services
        for service_name, service_desc in services.items():
            replicas = try_int(service_desc.get("deploy", {}).get("replicas"), fallback=1)

            container_names_by_service[service_name] = []
            for num in range(1, replicas + 1):
                name0 = f"{project_name}_{service_name}_{num}"
                if num == 1:
                    name = service_desc.get("container_name", name0)
                else:
                    name = name0
                container_names_by_service[service_name].append(name)
                # log(service_name,service_desc)
                cnt = {
                    "name": name,
                    "num": num,
                    "service_name": service_name,
                    **service_desc,
                }
                x_podman = service_desc.get("x-podman")
                rootfs_mode = x_podman is not None and x_podman.get("rootfs") is not None
                if "image" not in cnt and not rootfs_mode:
                    cnt["image"] = f"{project_name}_{service_name}"
                labels = norm_as_list(cnt.get("labels"))
                cnt["ports"] = norm_ports(cnt.get("ports"))
                labels.extend(podman_compose_labels)
                labels.extend([
                    f"com.docker.compose.container-number={num}",
                    "com.docker.compose.service=" + service_name,
                ])
                cnt["labels"] = labels
                cnt["_service"] = service_name
                cnt["_project"] = project_name
                given_containers.append(cnt)
                volumes = cnt.get("volumes", [])
                for volume in volumes:
                    mnt_dict = get_mnt_dict(self, cnt, volume)
                    if (
                        mnt_dict.get("type") == "volume"
                        and mnt_dict["source"]
                        and mnt_dict["source"] not in self.vols
                    ):
                        vol_name = mnt_dict["source"]
                        raise RuntimeError(f"volume [{vol_name}] not defined in top level")
        self.container_names_by_service = container_names_by_service
        self.all_services = set(container_names_by_service.keys())
        container_by_name = {c["name"]: c for c in given_containers}
        # log("deps:", [(c["name"], c["_deps"]) for c in given_containers])
        given_containers = list(container_by_name.values())
        given_containers.sort(key=lambda c: len(c.get("_deps", [])))
        # log("sorted:", [c["name"] for c in given_containers])

        self.x_podman = compose.get("x-podman", {})

        args.in_pod_bool = self.resolve_in_pod()
        pods, containers = transform(args, project_name, given_containers)
        self.pods = pods
        self.containers = containers
        self.container_by_name = {c["name"]: c for c in containers}

    def _resolve_profiles(self, defined_services, requested_profiles=None):
        """
        Returns a service dictionary (key = service name, value = service config) compatible with
        the requested_profiles list.

        The returned service dictionary contains all services which do not include/reference a
        profile in addition to services that match the requested_profiles.

        :param defined_services: The service dictionary
        :param requested_profiles: The profiles requested using the --profile arg.
        """
        if requested_profiles is None:
            requested_profiles = set()

        services = {}

        for name, config in defined_services.items():
            service_profiles = set(config.get("profiles", []))
            if not service_profiles or requested_profiles.intersection(service_profiles):
                services[name] = config
        return services

    def _parse_args(self, argv=None):
        parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter)
        self._init_global_parser(parser)
        subparsers = parser.add_subparsers(title="command", dest="command")
        subparser = subparsers.add_parser("help", help="show help")
        for cmd_name, cmd in self.commands.items():
            subparser = subparsers.add_parser(cmd_name, help=cmd.desc)  # pylint: disable=protected-access
            for cmd_parser in cmd._parse_args:  # pylint: disable=protected-access
                cmd_parser(subparser)
        self.global_args = parser.parse_args(argv)
        if self.global_args.in_pod is not None and self.global_args.in_pod.lower() not in (
            '',
            'true',
            '1',
            'false',
            '0',
        ):
            raise ValueError(
                f'Invalid --in-pod value: \'{self.global_args.in_pod}\'. '
                'It must be set to either of: empty value, true, 1, false, 0'
            )

        if self.global_args.in_pod == '' or self.global_args.in_pod is None:
            self.global_args.in_pod_bool = None
        else:
            self.global_args.in_pod_bool = self.global_args.in_pod.lower() in ('true', '1')

        if self.global_args.version:
            self.global_args.command = "version"
        if not self.global_args.command or self.global_args.command == "help":
            parser.print_help()
            sys.exit(-1)

        logging.basicConfig(level=("DEBUG" if self.global_args.verbose else "WARN"))
        return self.global_args

    @staticmethod
    def _init_global_parser(parser):
        parser.add_argument("-v", "--version", help="show version", action="store_true")
        parser.add_argument(
            "--in-pod",
            help="pod creation",
            metavar="in_pod",
            type=str,
            default=None,
        )
        parser.add_argument(
            "--pod-args",
            help="custom arguments to be passed to `podman pod`",
            metavar="pod_args",
            type=str,
            default="--infra=false --share=",
        )
        parser.add_argument(
            "--env-file",
            help="Specify an alternate environment file",
            metavar="env_file",
            type=str,
            default=".env",
        )
        parser.add_argument(
            "-f",
            "--file",
            help="Specify an compose file (default: docker-compose.yml) or '-' to read from stdin.",
            metavar="file",
            action="append",
            default=[],
        )
        parser.add_argument(
            "--profile",
            help="Specify a profile to enable",
            metavar="profile",
            action="append",
            default=[],
        )
        parser.add_argument(
            "-p",
            "--project-name",
            help="Specify an alternate project name (default: directory name)",
            type=str,
            default=None,
        )
        parser.add_argument(
            "--podman-path",
            help="Specify an alternate path to podman (default: use location in $PATH variable)",
            type=str,
            default="podman",
        )
        parser.add_argument(
            "--podman-args",
            help="custom global arguments to be passed to `podman`",
            metavar="args",
            action="append",
            default=[],
        )
        for podman_cmd in PODMAN_CMDS:
            parser.add_argument(
                f"--podman-{podman_cmd}-args",
                help=f"custom arguments to be passed to `podman {podman_cmd}`",
                metavar="args",
                action="append",
                default=[],
            )
        parser.add_argument(
            "--no-ansi",
            help="Do not print ANSI control characters",
            action="store_true",
        )
        parser.add_argument(
            "--no-cleanup",
            help="Do not stop and remove existing pod & containers",
            action="store_true",
        )
        parser.add_argument(
            "--dry-run",
            help="No action; perform a simulation of commands",
            action="store_true",
        )
        parser.add_argument(
            "--parallel", type=int, default=os.environ.get("COMPOSE_PARALLEL_LIMIT", sys.maxsize)
        )
        parser.add_argument(
            "--verbose",
            help="Print debugging output",
            action="store_true",
        )


podman_compose = PodmanCompose()


###################
# decorators to add commands and parse options
###################
class PodmanComposeError(Exception):
    pass


class cmd_run:  # pylint: disable=invalid-name,too-few-public-methods
    def __init__(self, compose, cmd_name, cmd_desc=None):
        self.compose = compose
        self.cmd_name = cmd_name
        self.cmd_desc = cmd_desc

    def __call__(self, func):
        def wrapped(*args, **kw):
            return func(*args, **kw)

        if not asyncio.iscoroutinefunction(func):
            raise PodmanComposeError("Command must be async")
        wrapped._compose = self.compose
        # Trim extra indentation at start of multiline docstrings.
        wrapped.desc = self.cmd_desc or re.sub(r"^\s+", "", func.__doc__)
        wrapped._parse_args = []
        self.compose.commands[self.cmd_name] = wrapped
        return wrapped


class cmd_parse:  # pylint: disable=invalid-name,too-few-public-methods
    def __init__(self, compose, cmd_names):
        self.compose = compose
        self.cmd_names = cmd_names if is_list(cmd_names) else [cmd_names]

    def __call__(self, func):
        def wrapped(*args, **kw):
            return func(*args, **kw)

        for cmd_name in self.cmd_names:
            self.compose.commands[cmd_name]._parse_args.append(wrapped)
        return wrapped


###################
# actual commands
###################


@cmd_run(podman_compose, "version", "show version")
async def compose_version(compose, args):
    if getattr(args, "short", False):
        print(__version__)
        return
    if getattr(args, "format", "pretty") == "json":
        res = {"version": __version__}
        print(json.dumps(res))
        return
    print("podman-compose version", __version__)
    await compose.podman.run(["--version"], "", [])


def is_local(container: dict) -> bool:
    """Test if a container is local, i.e. if it is
    * prefixed with localhost/
    * has a build section and is not prefixed
    """
    return (
        "/" not in container["image"]
        if "build" in container
        else container["image"].startswith("localhost/")
    )


@cmd_run(podman_compose, "wait", "wait running containers to stop")
async def compose_wait(compose, args):  # pylint: disable=unused-argument
    containers = [cnt["name"] for cnt in compose.containers]
    cmd_args = ["--"]
    cmd_args.extend(containers)
    await compose.podman.exec([], "wait", cmd_args)


@cmd_run(podman_compose, "systemd")
async def compose_systemd(compose, args):
    """
    create systemd unit file and register its compose stacks

    When first installed type `sudo podman-compose systemd -a create-unit`
    later you can add a compose stack by running `podman-compose systemd -a register`
    then you can start/stop your stack with `systemctl --user start podman-compose@<PROJ>`
    """
    stacks_dir = ".config/containers/compose/projects"
    if args.action == "register":
        proj_name = compose.project_name
        fn = os.path.expanduser(f"~/{stacks_dir}/{proj_name}.env")
        os.makedirs(os.path.dirname(fn), exist_ok=True)
        log.debug("writing [%s]: ...", fn)
        with open(fn, "w", encoding="utf-8") as f:
            for k, v in compose.environ.items():
                if k.startswith("COMPOSE_") or k.startswith("PODMAN_"):
                    f.write(f"{k}={v}\n")
        log.debug("writing [%s]: done.", fn)
        log.info("\n\ncreating the pod without starting it: ...\n\n")
        username = getpass.getuser()
        print(
            f"""
you can use systemd commands like enable, start, stop, status, cat
all without `sudo` like this:

\t\tsystemctl --user enable --now 'podman-compose@{proj_name}'
\t\tsystemctl --user status 'podman-compose@{proj_name}'
\t\tjournalctl --user -xeu 'podman-compose@{proj_name}'

and for that to work outside a session
you might need to run the following command *once*

\t\tsudo loginctl enable-linger '{username}'

you can use podman commands like:

\t\tpodman pod ps
\t\tpodman pod stats 'pod_{proj_name}'
\t\tpodman pod logs --tail=10 -f 'pod_{proj_name}'
"""
        )
    elif args.action in ("list", "ls"):
        ls = glob.glob(os.path.expanduser(f"~/{stacks_dir}/*.env"))
        for i in ls:
            print(os.path.basename(i[:-4]))
    elif args.action == "create-unit":
        fn = "/etc/systemd/user/podman-compose@.service"
        out = f"""\
# {fn}

[Unit]
Description=%i rootless pod (podman-compose)

[Service]
Type=simple
EnvironmentFile=%h/{stacks_dir}/%i.env
ExecStartPre=-{script} up --no-start
ExecStartPre=/usr/bin/podman pod start pod_%i
ExecStart={script} wait
ExecStop=/usr/bin/podman pod stop pod_%i

[Install]
WantedBy=default.target
"""
        if os.access(os.path.dirname(fn), os.W_OK):
            log.debug("writing [%s]: ...", fn)
            with open(fn, "w", encoding="utf-8") as f:
                f.write(out)
            log.debug("writing [%s]: done.", fn)
            print(
                """
while in your project type `podman-compose systemd -a register`
"""
            )
        else:
            print(out)
            log.warning("Could not write to [%s], use 'sudo'", fn)


@cmd_run(podman_compose, "pull", "pull stack images")
async def compose_pull(compose, args):
    img_containers = [cnt for cnt in compose.containers if "image" in cnt]
    if args.services:
        services = set(args.services)
        img_containers = [cnt for cnt in img_containers if cnt["_service"] in services]
    images = {cnt["image"] for cnt in img_containers}
    if not args.force_local:
        local_images = {cnt["image"] for cnt in img_containers if is_local(cnt)}
        images -= local_images

    await asyncio.gather(*[compose.podman.run([], "pull", [image]) for image in images])


@cmd_run(podman_compose, "push", "push stack images")
async def compose_push(compose, args):
    services = set(args.services)
    for cnt in compose.containers:
        if "build" not in cnt:
            continue
        if services and cnt["_service"] not in services:
            continue
        await compose.podman.run([], "push", [cnt["image"]])


async def build_one(compose, args, cnt):
    if "build" not in cnt:
        return None
    if getattr(args, "if_not_exists", None):
        try:
            img_id = await compose.podman.output(
                [], "inspect", ["-t", "image", "-f", "{{.Id}}", cnt["image"]]
            )
        except subprocess.CalledProcessError:
            img_id = None
        if img_id:
            return None
    build_desc = cnt["build"]
    if not hasattr(build_desc, "items"):
        build_desc = {"context": build_desc}
    ctx = build_desc.get("context", ".")
    dockerfile = build_desc.get("dockerfile")
    if dockerfile:
        dockerfile = os.path.join(ctx, dockerfile)
    else:
        dockerfile_alts = [
            "Containerfile",
            "ContainerFile",
            "containerfile",
            "Dockerfile",
            "DockerFile",
            "dockerfile",
        ]
        for dockerfile in dockerfile_alts:
            dockerfile = os.path.join(ctx, dockerfile)
            if os.path.exists(dockerfile):
                break
    if not os.path.exists(dockerfile):
        raise OSError("Dockerfile not found in " + ctx)
    build_args = ["-f", dockerfile, "-t", cnt["image"]]
    if "platform" in cnt:
        build_args.extend(["--platform", cnt["platform"]])
    for secret in build_desc.get("secrets", []):
        build_args.extend(get_secret_args(compose, cnt, secret, podman_is_building=True))
    for tag in build_desc.get("tags", []):
        build_args.extend(["-t", tag])
    labels = build_desc.get("labels", [])
    if isinstance(labels, dict):
        labels = [f"{k}={v}" for (k, v) in labels.items()]
    for label in labels:
        build_args.extend(["--label", label])
    for additional_ctx in build_desc.get("additional_contexts", {}):
        build_args.extend([f"--build-context={additional_ctx}"])
    if "target" in build_desc:
        build_args.extend(["--target", build_desc["target"]])
    for agent_or_key in norm_as_list(build_desc.get("ssh", {})):
        build_args.extend(["--ssh", agent_or_key])
    container_to_ulimit_build_args(cnt, build_args)
    if getattr(args, "no_cache", None):
        build_args.append("--no-cache")
    if getattr(args, "pull_always", None):
        build_args.append("--pull-always")
    elif getattr(args, "pull", None):
        build_args.append("--pull")
    args_list = norm_as_list(build_desc.get("args", {}))
    for build_arg in args_list + args.build_arg:
        build_args.extend((
            "--build-arg",
            build_arg,
        ))
    build_args.append(ctx)
    status = await compose.podman.run([], "build", build_args)
    return status


@cmd_run(podman_compose, "build", "build stack images")
async def compose_build(compose, args):
    tasks = []

    if args.services:
        container_names_by_service = compose.container_names_by_service
        compose.assert_services(args.services)
        for service in args.services:
            cnt = compose.container_by_name[container_names_by_service[service][0]]
            tasks.append(asyncio.create_task(build_one(compose, args, cnt)))

    else:
        for cnt in compose.containers:
            tasks.append(asyncio.create_task(build_one(compose, args, cnt)))

    status = 0
    for t in asyncio.as_completed(tasks):
        s = await t
        if s is not None:
            status = s

    return status


async def pod_exists(compose, name):
    exit_code = await compose.podman.run([], "pod", ["exists", name])
    return exit_code == 0


async def create_pods(compose, args):  # pylint: disable=unused-argument
    for pod in compose.pods:
        if await pod_exists(compose, pod["name"]):
            continue

        podman_args = [
            "create",
            "--name=" + pod["name"],
        ]
        if args.pod_args:
            podman_args.extend(shlex.split(args.pod_args))
        # if compose.podman_version and not strverscmp_lt(compose.podman_version, "3.4.0"):
        #    podman_args.append("--infra-name={}_infra".format(pod["name"]))
        ports = pod.get("ports", [])
        if isinstance(ports, str):
            ports = [ports]
        for i in ports:
            podman_args.extend(["-p", str(i)])
        await compose.podman.run([], "pod", podman_args)


def get_excluded(compose, args):
    excluded = set()
    if args.services:
        excluded = set(compose.services)
        for service in args.services:
            excluded -= set(x.name for x in compose.services[service]["_deps"])
            excluded.discard(service)
    log.debug("** excluding: %s", excluded)
    return excluded


async def check_dep_conditions(compose: PodmanCompose, deps: set) -> None:
    """Enforce that all specified conditions in deps are met"""
    if not deps:
        return

    for condition in ServiceDependencyCondition:
        deps_cd = []
        for d in deps:
            if d.condition == condition:
                deps_cd.extend(compose.container_names_by_service[d.name])

        if deps_cd:
            # podman wait will return always with a rc -1.
            while True:
                try:
                    await compose.podman.output(
                        [], "wait", [f"--condition={condition.value}"] + deps_cd
                    )
                    log.debug(
                        "dependencies for condition %s have been fulfilled on containers %s",
                        condition.value,
                        ', '.join(deps_cd),
                    )
                    break
                except subprocess.CalledProcessError as _exc:
                    output = list(
                        ((_exc.stdout or b"") + (_exc.stderr or b"")).decode().split('\n')
                    )
                    log.debug(
                        'Podman wait returned an error (%d) when executing "%s": %s',
                        _exc.returncode,
                        _exc.cmd,
                        output,
                    )
                await asyncio.sleep(1)


async def run_container(
    compose: PodmanCompose, name: str, deps: set, command: tuple, log_formatter: str = None
):
    """runs a container after waiting for its dependencies to be fulfilled"""

    # wait for the dependencies to be fulfilled
    if "start" in command:
        log.debug("Checking dependencies prior to container %s start", name)
        await check_dep_conditions(compose, deps)

    # start the container
    log.debug("Starting task for container %s", name)
    return await compose.podman.run(*command, log_formatter=log_formatter)


@cmd_run(podman_compose, "up", "Create and start the entire stack or some of its services")
async def compose_up(compose: PodmanCompose, args):
    excluded = get_excluded(compose, args)
    if not args.no_build:
        # `podman build` does not cache, so don't always build
        build_args = argparse.Namespace(if_not_exists=(not args.build), **args.__dict__)
        if await compose.commands["build"](compose, build_args) != 0:
            log.error("Build command failed")

    hashes = (
        (
            await compose.podman.output(
                [],
                "ps",
                [
                    "--filter",
                    f"label=io.podman.compose.project={compose.project_name}",
                    "-a",
                    "--format",
                    '{{ index .Labels "io.podman.compose.config-hash"}}',
                ],
            )
        )
        .decode("utf-8")
        .splitlines()
    )
    diff_hashes = [i for i in hashes if i and i != compose.yaml_hash]
    if args.force_recreate or len(diff_hashes):
        log.info("recreating: ...")
        down_args = argparse.Namespace(**dict(args.__dict__, volumes=False))
        await compose.commands["down"](compose, down_args)
        log.info("recreating: done\n\n")
    # args.no_recreate disables check for changes (which is not implemented)

    podman_command = "run" if args.detach and not args.no_start else "create"

    await create_pods(compose, args)
    for cnt in compose.containers:
        if cnt["_service"] in excluded:
            log.debug("** skipping: %s", cnt["name"])
            continue
        podman_args = await container_to_args(compose, cnt, detached=args.detach)
        subproc = await compose.podman.run([], podman_command, podman_args)
        if podman_command == "run" and subproc is not None:
            await run_container(compose, cnt["name"], cnt["_deps"], ([], "start", [cnt["name"]]))
    if args.no_start or args.detach or args.dry_run:
        return
    # TODO: handle already existing
    # TODO: if error creating do not enter loop
    # TODO: colors if sys.stdout.isatty()
    exit_code_from = args.__dict__.get("exit_code_from")
    if exit_code_from:
        args.abort_on_container_exit = True

    max_service_length = 0
    for cnt in compose.containers:
        curr_length = len(cnt["_service"])
        max_service_length = curr_length if curr_length > max_service_length else max_service_length

    tasks = set()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, lambda: [t.cancel("User exit") for t in tasks])

    for i, cnt in enumerate(compose.containers):
        # Add colored service prefix to output by piping output through sed
        color_idx = i % len(compose.console_colors)
        color = compose.console_colors[color_idx]
        space_suffix = " " * (max_service_length - len(cnt["_service"]) + 1)
        log_formatter = "{}[{}]{}|\x1b[0m".format(color, cnt["_service"], space_suffix)
        if cnt["_service"] in excluded:
            log.debug("** skipping: %s", cnt["name"])
            continue

        tasks.add(
            asyncio.create_task(
                run_container(
                    compose,
                    cnt["name"],
                    cnt["_deps"],
                    ([], "start", ["-a", cnt["name"]]),
                    log_formatter=log_formatter,
                ),
                name=cnt["_service"],
            )
        )

    def _task_cancelled(task: Task) -> bool:
        if task.cancelled():
            return True
        # Task.cancelling() is new in python 3.11
        if sys.version_info >= (3, 11) and task.cancelling():
            return True
        return False

    exit_code = 0
    exiting = False
    while tasks:
        done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if args.abort_on_container_exit:
            if not exiting:
                # If 2 containers exit at the exact same time, the cancellation of the other ones
                # cause the status to overwrite. Sleeping for 1 seems to fix this and make it match
                # docker-compose
                await asyncio.sleep(1)
                for t in tasks:
                    if not _task_cancelled(t):
                        t.cancel()
            t: Task
            exiting = True
            for t in done:
                if t.get_name() == exit_code_from:
                    exit_code = t.result()

    return exit_code


def get_volume_names(compose, cnt):
    basedir = compose.dirname
    srv_name = cnt["_service"]
    ls = []
    for volume in cnt.get("volumes", []):
        if isinstance(volume, str):
            volume = parse_short_mount(volume, basedir)
        volume = fix_mount_dict(compose, volume, srv_name)
        mount_type = volume["type"]
        if mount_type != "volume":
            continue
        volume_name = volume.get("_vol", {}).get("name")
        ls.append(volume_name)
    return ls


@cmd_run(podman_compose, "down", "tear down entire stack")
async def compose_down(compose: PodmanCompose, args):
    excluded = get_excluded(compose, args)
    podman_args = []
    timeout_global = getattr(args, "timeout", None)
    containers = list(reversed(compose.containers))

    down_tasks = []

    for cnt in containers:
        if cnt["_service"] in excluded:
            continue
        podman_stop_args = [*podman_args]
        timeout = timeout_global
        if timeout is None:
            timeout_str = cnt.get("stop_grace_period", STOP_GRACE_PERIOD)
            timeout = str_to_seconds(timeout_str)
        if timeout is not None:
            podman_stop_args.extend(["-t", str(timeout)])
        down_tasks.append(
            asyncio.create_task(
                compose.podman.run([], "stop", [*podman_stop_args, cnt["name"]]), name=cnt["name"]
            )
        )
    await asyncio.gather(*down_tasks)
    for cnt in containers:
        if cnt["_service"] in excluded:
            continue
        await compose.podman.run([], "rm", [cnt["name"]])
    if args.remove_orphans:
        names = (
            (
                await compose.podman.output(
                    [],
                    "ps",
                    [
                        "--filter",
                        f"label=io.podman.compose.project={compose.project_name}",
                        "-a",
                        "--format",
                        "{{ .Names }}",
                    ],
                )
            )
            .decode("utf-8")
            .splitlines()
        )
        for name in names:
            await compose.podman.run([], "stop", [*podman_args, name])
        for name in names:
            await compose.podman.run([], "rm", [name])
    if args.volumes:
        vol_names_to_keep = set()
        for cnt in containers:
            if cnt["_service"] not in excluded:
                continue
            vol_names_to_keep.update(get_volume_names(compose, cnt))
        log.debug("keep %s", vol_names_to_keep)
        for volume_name in await compose.podman.volume_ls():
            if volume_name in vol_names_to_keep:
                continue
            await compose.podman.run([], "volume", ["rm", volume_name])

    if excluded:
        return
    for pod in compose.pods:
        await compose.podman.run([], "pod", ["rm", pod["name"]])
    for network in await compose.podman.network_ls():
        await compose.podman.run([], "network", ["rm", network])


@cmd_run(podman_compose, "ps", "show status of containers")
async def compose_ps(compose, args):
    ps_args = ["-a", "--filter", f"label=io.podman.compose.project={compose.project_name}"]
    if args.quiet is True:
        ps_args.extend(["--format", "{{.ID}}"])
    elif args.format:
        ps_args.extend(["--format", args.format])

    await compose.podman.run(
        [],
        "ps",
        ps_args,
    )


@cmd_run(
    podman_compose,
    "run",
    "create a container similar to a service to run a one-off command",
)
async def compose_run(compose, args):
    await create_pods(compose, args)
    compose.assert_services(args.service)
    container_names = compose.container_names_by_service[args.service]
    container_name = container_names[0]
    cnt = dict(compose.container_by_name[container_name])
    deps = cnt["_deps"]
    if deps and not args.no_deps:
        up_args = argparse.Namespace(
            **dict(
                args.__dict__,
                detach=True,
                services=[x.name for x in deps],
                # defaults
                no_build=False,
                build=None,
                force_recreate=False,
                no_start=False,
                no_cache=False,
                build_arg=[],
                parallel=1,
                remove_orphans=True,
            )
        )
        await compose.commands["up"](compose, up_args)

    build_args = argparse.Namespace(
        services=[args.service], if_not_exists=(not args.build), build_arg=[], **args.__dict__
    )
    await compose.commands["build"](compose, build_args)

    compose_run_update_container_from_args(compose, cnt, args)
    # run podman
    podman_args = await container_to_args(compose, cnt, args.detach)
    if not args.detach:
        podman_args.insert(1, "-i")
        if args.rm:
            podman_args.insert(1, "--rm")
    p = await compose.podman.run([], "run", podman_args)
    sys.exit(p)


def compose_run_update_container_from_args(compose, cnt, args):
    # adjust one-off container options
    name0 = "{}_{}_tmp{}".format(compose.project_name, args.service, random.randrange(0, 65536))
    cnt["name"] = args.name or name0
    if args.entrypoint:
        cnt["entrypoint"] = args.entrypoint
    if args.user:
        cnt["user"] = args.user
    if args.workdir:
        cnt["working_dir"] = args.workdir
    env = dict(cnt.get("environment", {}))
    if args.env:
        additional_env_vars = dict(map(lambda each: each.split("=", maxsplit=1), args.env))
        env.update(additional_env_vars)
        cnt["environment"] = env
    if not args.service_ports:
        for k in ("expose", "publishall", "ports"):
            try:
                del cnt[k]
            except KeyError:
                pass
    if args.publish:
        ports = cnt.get("ports", [])
        ports.extend(norm_ports(args.publish))
        cnt["ports"] = ports
    if args.volume:
        # TODO: handle volumes
        volumes = clone(cnt.get("volumes", []))
        volumes.extend(args.volume)
        cnt["volumes"] = volumes
    cnt["tty"] = not args.T
    if args.cnt_command is not None and len(args.cnt_command) > 0:
        cnt["command"] = args.cnt_command
    # can't restart and --rm
    if args.rm and "restart" in cnt:
        del cnt["restart"]


@cmd_run(podman_compose, "exec", "execute a command in a running container")
async def compose_exec(compose, args):
    compose.assert_services(args.service)
    container_names = compose.container_names_by_service[args.service]
    container_name = container_names[args.index - 1]
    cnt = compose.container_by_name[container_name]
    podman_args = compose_exec_args(cnt, container_name, args)
    p = await compose.podman.run([], "exec", podman_args)
    sys.exit(p)


def compose_exec_args(cnt, container_name, args):
    podman_args = ["--interactive"]
    if args.privileged:
        podman_args += ["--privileged"]
    if args.user:
        podman_args += ["--user", args.user]
    if args.workdir:
        podman_args += ["--workdir", args.workdir]
    if not args.T:
        podman_args += ["--tty"]
    env = dict(cnt.get("environment", {}))
    if args.env:
        additional_env_vars = dict(
            map(lambda each: each.split("=", maxsplit=1) if "=" in each else (each, None), args.env)
        )
        env.update(additional_env_vars)
    for name, value in env.items():
        podman_args += ["--env", f"{name}" if value is None else f"{name}={value}"]
    podman_args += [container_name]
    if args.cnt_command is not None and len(args.cnt_command) > 0:
        podman_args += args.cnt_command
    return podman_args


async def transfer_service_status(compose, args, action):
    # TODO: handle dependencies, handle creations
    container_names_by_service = compose.container_names_by_service
    if not args.services:
        args.services = container_names_by_service.keys()
    compose.assert_services(args.services)
    targets = []
    for service in args.services:
        if service not in container_names_by_service:
            raise ValueError("unknown service: " + service)
        targets.extend(container_names_by_service[service])
    if action in ["stop", "restart"]:
        targets = list(reversed(targets))
    timeout_global = getattr(args, "timeout", None)
    tasks = []
    for target in targets:
        podman_args = []
        if action != "start":
            timeout = timeout_global
            if timeout is None:
                timeout_str = compose.container_by_name[target].get(
                    "stop_grace_period", STOP_GRACE_PERIOD
                )
                timeout = str_to_seconds(timeout_str)
            if timeout is not None:
                podman_args.extend(["-t", str(timeout)])
        tasks.append(asyncio.create_task(compose.podman.run([], action, podman_args + [target])))
    await asyncio.gather(*tasks)


@cmd_run(podman_compose, "start", "start specific services")
async def compose_start(compose, args):
    await transfer_service_status(compose, args, "start")


@cmd_run(podman_compose, "stop", "stop specific services")
async def compose_stop(compose, args):
    await transfer_service_status(compose, args, "stop")


@cmd_run(podman_compose, "restart", "restart specific services")
async def compose_restart(compose, args):
    await transfer_service_status(compose, args, "restart")


@cmd_run(podman_compose, "logs", "show logs from services")
async def compose_logs(compose, args):
    container_names_by_service = compose.container_names_by_service
    if not args.services and not args.latest:
        args.services = container_names_by_service.keys()
    compose.assert_services(args.services)
    targets = []
    for service in args.services:
        targets.extend(container_names_by_service[service])
    podman_args = []
    if args.follow:
        podman_args.append("-f")
    if args.latest:
        podman_args.append("-l")
    if args.names:
        podman_args.append("-n")
    if args.since:
        podman_args.extend(["--since", args.since])
    # the default value is to print all logs which is in podman = 0 and not
    # needed to be passed
    if args.tail and args.tail != "all":
        podman_args.extend(["--tail", args.tail])
    if args.timestamps:
        podman_args.append("-t")
    if args.until:
        podman_args.extend(["--until", args.until])
    for target in targets:
        podman_args.append(target)
    await compose.podman.run([], "logs", podman_args)


@cmd_run(podman_compose, "config", "displays the compose file")
async def compose_config(compose, args):
    if args.services:
        for service in compose.services:
            print(service)
        return
    print(compose.merged_yaml)


@cmd_run(podman_compose, "port", "Prints the public port for a port binding.")
async def compose_port(compose, args):
    # TODO - deal with pod index
    compose.assert_services(args.service)
    containers = compose.container_names_by_service[args.service]
    container_ports = list(
        itertools.chain(*(compose.container_by_name[c]["ports"] for c in containers))
    )

    def _published_target(port_string):
        published, target = port_string.split(":")[-2:]
        return int(published), int(target)

    select_udp = args.protocol == "udp"
    published, target = None, None
    for p in container_ports:
        is_udp = p[-4:] == "/udp"

        if select_udp and is_udp:
            published, target = _published_target(p[-4:])
        if not select_udp and not is_udp:
            published, target = _published_target(p)

        if target == args.private_port:
            print(published)
            return


@cmd_run(podman_compose, "pause", "Pause all running containers")
async def compose_pause(compose, args):
    container_names_by_service = compose.container_names_by_service
    if not args.services:
        args.services = container_names_by_service.keys()
    targets = []
    for service in args.services:
        targets.extend(container_names_by_service[service])
    await compose.podman.run([], "pause", targets)


@cmd_run(podman_compose, "unpause", "Unpause all running containers")
async def compose_unpause(compose, args):
    container_names_by_service = compose.container_names_by_service
    if not args.services:
        args.services = container_names_by_service.keys()
    targets = []
    for service in args.services:
        targets.extend(container_names_by_service[service])
    await compose.podman.run([], "unpause", targets)


@cmd_run(podman_compose, "kill", "Kill one or more running containers with a specific signal")
async def compose_kill(compose, args):
    # to ensure that the user did not execute the command by mistake
    if not args.services and not args.all:
        log.fatal(
            "Error: you must provide at least one service name or use (--all) to kill all services"
        )
        sys.exit()

    container_names_by_service = compose.container_names_by_service
    podman_args = []

    if args.signal:
        podman_args.extend(["--signal", args.signal])

    if args.all is True:
        services = container_names_by_service.keys()
        targets = []
        for service in services:
            targets.extend(container_names_by_service[service])
        for target in targets:
            podman_args.append(target)
        await compose.podman.run([], "kill", podman_args)
    elif args.services:
        targets = []
        for service in args.services:
            targets.extend(container_names_by_service[service])
        for target in targets:
            podman_args.append(target)
        await compose.podman.run([], "kill", podman_args)


@cmd_run(
    podman_compose,
    "stats",
    "Display percentage of CPU, memory, network I/O, block I/O and PIDs for services.",
)
async def compose_stats(compose, args):
    container_names_by_service = compose.container_names_by_service
    if not args.services:
        args.services = container_names_by_service.keys()
    targets = []
    podman_args = []
    if args.interval:
        podman_args.extend(["--interval", args.interval])
    if args.format:
        podman_args.extend(["--format", args.format])
    if args.no_reset:
        podman_args.append("--no-reset")
    if args.no_stream:
        podman_args.append("--no-stream")

    for service in args.services:
        targets.extend(container_names_by_service[service])
    for target in targets:
        podman_args.append(target)

    try:
        await compose.podman.run([], "stats", podman_args)
    except KeyboardInterrupt:
        pass


@cmd_run(podman_compose, "images", "List images used by the created containers")
async def compose_images(compose, args):
    img_containers = [cnt for cnt in compose.containers if "image" in cnt]
    data = []
    if args.quiet is True:
        for img in img_containers:
            name = img["name"]
            output = await compose.podman.output([], "images", ["--quiet", img["image"]])
            data.append(output.decode("utf-8").split())
    else:
        data.append(["CONTAINER", "REPOSITORY", "TAG", "IMAGE ID", "SIZE", ""])
        for img in img_containers:
            name = img["name"]
            output = await compose.podman.output(
                [],
                "images",
                [
                    "--format",
                    "table " + name + " {{.Repository}} {{.Tag}} {{.ID}} {{.Size}}",
                    "-n",
                    img["image"],
                ],
            )
            data.append(output.decode("utf-8").split())

    # Determine the maximum length of each column
    column_widths = [max(map(len, column)) for column in zip(*data)]

    # Print each row
    for row in data:
        # Format each cell using the maximum column width
        formatted_row = [cell.ljust(width) for cell, width in zip(row, column_widths)]
        formatted_row[-2:] = ["".join(formatted_row[-2:]).strip()]
        print("\t".join(formatted_row))


###################
# command arguments parsing
###################


@cmd_parse(podman_compose, "version")
def compose_version_parse(parser):
    parser.add_argument(
        "-f",
        "--format",
        choices=["pretty", "json"],
        default="pretty",
        help="Format the output",
    )
    parser.add_argument(
        "--short",
        action="store_true",
        help="Shows only Podman Compose's version number",
    )


@cmd_parse(podman_compose, "up")
def compose_up_parse(parser):
    parser.add_argument(
        "-d",
        "--detach",
        action="store_true",
        help="Detached mode: Run container in the background, print new container name. \
            Incompatible with --abort-on-container-exit.",
    )
    parser.add_argument("--no-color", action="store_true", help="Produce monochrome output.")
    parser.add_argument(
        "--quiet-pull",
        action="store_true",
        help="Pull without printing progress information.",
    )
    parser.add_argument("--no-deps", action="store_true", help="Don't start linked services.")
    parser.add_argument(
        "--force-recreate",
        action="store_true",
        help="Recreate containers even if their configuration and image haven't changed.",
    )
    parser.add_argument(
        "--always-recreate-deps",
        action="store_true",
        help="Recreate dependent containers. Incompatible with --no-recreate.",
    )
    parser.add_argument(
        "--no-recreate",
        action="store_true",
        help="If containers already exist, don't recreate them. Incompatible with --force-recreate "
        "and -V.",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="Don't build an image, even if it's missing.",
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="Don't start the services after creating them.",
    )
    parser.add_argument(
        "--build", action="store_true", help="Build images before starting containers."
    )
    parser.add_argument(
        "--abort-on-container-exit",
        action="store_true",
        help="Stops all containers if any container was stopped. Incompatible with -d.",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=int,
        default=None,
        help="Use this timeout in seconds for container shutdown when attached or when containers "
        "are already running. (default: 10)",
    )
    parser.add_argument(
        "-V",
        "--renew-anon-volumes",
        action="store_true",
        help="Recreate anonymous volumes instead of retrieving data from the previous containers.",
    )
    parser.add_argument(
        "--remove-orphans",
        action="store_true",
        help="Remove containers for services not defined in the Compose file.",
    )
    parser.add_argument(
        "--scale",
        metavar="SERVICE=NUM",
        action="append",
        help="Scale SERVICE to NUM instances. Overrides the `scale` setting in the Compose file if "
        "present.",
    )
    parser.add_argument(
        "--exit-code-from",
        metavar="SERVICE",
        type=str,
        default=None,
        help="Return the exit code of the selected service container. "
        "Implies --abort-on-container-exit.",
    )


@cmd_parse(podman_compose, "down")
def compose_down_parse(parser):
    parser.add_argument(
        "-v",
        "--volumes",
        action="store_true",
        default=False,
        help="Remove named volumes declared in the `volumes` section of the Compose file and "
        "anonymous volumes attached to containers.",
    )
    parser.add_argument(
        "--remove-orphans",
        action="store_true",
        help="Remove containers for services not defined in the Compose file.",
    )


@cmd_parse(podman_compose, "run")
def compose_run_parse(parser):
    parser.add_argument(
        "--build", action="store_true", help="Build images before starting containers."
    )
    parser.add_argument(
        "-d",
        "--detach",
        action="store_true",
        help="Detached mode: Run container in the background, print new container name.",
    )
    parser.add_argument("--name", type=str, default=None, help="Assign a name to the container")
    parser.add_argument(
        "--entrypoint",
        type=str,
        default=None,
        help="Override the entrypoint of the image.",
    )
    parser.add_argument(
        "-e",
        "--env",
        metavar="KEY=VAL",
        action="append",
        help="Set an environment variable (can be used multiple times)",
    )
    parser.add_argument(
        "-l",
        "--label",
        metavar="KEY=VAL",
        action="append",
        help="Add or override a label (can be used multiple times)",
    )
    parser.add_argument(
        "-u", "--user", type=str, default=None, help="Run as specified username or uid"
    )
    parser.add_argument("--no-deps", action="store_true", help="Don't start linked services")
    parser.add_argument(
        "--rm",
        action="store_true",
        help="Remove container after run. Ignored in detached mode.",
    )
    parser.add_argument(
        "-p",
        "--publish",
        action="append",
        help="Publish a container's port(s) to the host (can be used multiple times)",
    )
    parser.add_argument(
        "--service-ports",
        action="store_true",
        help="Run command with the service's ports enabled and mapped to the host.",
    )
    parser.add_argument(
        "-v",
        "--volume",
        action="append",
        help="Bind mount a volume (can be used multiple times)",
    )
    parser.add_argument(
        "-T",
        action="store_true",
        help="Disable pseudo-tty allocation. By default `podman-compose run` allocates a TTY.",
    )
    parser.add_argument(
        "-w",
        "--workdir",
        type=str,
        default=None,
        help="Working directory inside the container",
    )
    parser.add_argument("service", metavar="service", nargs=None, help="service name")
    parser.add_argument(
        "cnt_command",
        metavar="command",
        nargs=argparse.REMAINDER,
        help="command and its arguments",
    )


@cmd_parse(podman_compose, "exec")
def compose_exec_parse(parser):
    parser.add_argument(
        "-d",
        "--detach",
        action="store_true",
        help="Detached mode: Run container in the background, print new container name.",
    )
    parser.add_argument(
        "--privileged",
        action="store_true",
        default=False,
        help="Give the process extended Linux capabilities inside the container",
    )
    parser.add_argument(
        "-u", "--user", type=str, default=None, help="Run as specified username or uid"
    )
    parser.add_argument(
        "-T",
        action="store_true",
        help="Disable pseudo-tty allocation. By default `podman-compose run` allocates a TTY.",
    )
    parser.add_argument(
        "--index",
        type=int,
        default=1,
        help="Index of the container if there are multiple instances of a service",
    )
    parser.add_argument(
        "-e",
        "--env",
        metavar="KEY=VAL",
        action="append",
        help="Set an environment variable (can be used multiple times)",
    )
    parser.add_argument(
        "-w",
        "--workdir",
        type=str,
        default=None,
        help="Working directory inside the container",
    )
    parser.add_argument("service", metavar="service", nargs=None, help="service name")
    parser.add_argument(
        "cnt_command",
        metavar="command",
        nargs=argparse.REMAINDER,
        help="command and its arguments",
    )


@cmd_parse(podman_compose, ["down", "stop", "restart"])
def compose_parse_timeout(parser):
    parser.add_argument(
        "-t",
        "--timeout",
        help="Specify a shutdown timeout in seconds. ",
        type=int,
        default=None,
    )


@cmd_parse(podman_compose, ["logs"])
def compose_logs_parse(parser):
    parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="Follow log output. The default is false",
    )
    parser.add_argument(
        "-l",
        "--latest",
        action="store_true",
        help="Act on the latest container podman is aware of",
    )
    parser.add_argument(
        "-n",
        "--names",
        action="store_true",
        help="Output the container name in the log",
    )
    parser.add_argument("--since", help="Show logs since TIMESTAMP", type=str, default=None)
    parser.add_argument("-t", "--timestamps", action="store_true", help="Show timestamps.")
    parser.add_argument(
        "--tail",
        help="Number of lines to show from the end of the logs for each container.",
        type=str,
        default="all",
    )
    parser.add_argument("--until", help="Show logs until TIMESTAMP", type=str, default=None)
    parser.add_argument(
        "services", metavar="services", nargs="*", default=None, help="service names"
    )


@cmd_parse(podman_compose, "systemd")
def compose_systemd_parse(parser):
    parser.add_argument(
        "-a",
        "--action",
        choices=["register", "create-unit", "list", "ls"],
        default="register",
        help="create systemd unit file or register compose stack to it",
    )


@cmd_parse(podman_compose, "pull")
def compose_pull_parse(parser):
    parser.add_argument(
        "--force-local",
        action="store_true",
        default=False,
        help="Also pull unprefixed images for services which have a build section",
    )
    parser.add_argument("services", metavar="services", nargs="*", help="services to pull")


@cmd_parse(podman_compose, "push")
def compose_push_parse(parser):
    parser.add_argument(
        "--ignore-push-failures",
        action="store_true",
        help="Push what it can and ignores images with push failures. (not implemented)",
    )
    parser.add_argument("services", metavar="services", nargs="*", help="services to push")


@cmd_parse(podman_compose, "ps")
def compose_ps_parse(parser):
    parser.add_argument("-q", "--quiet", help="Only display container IDs", action="store_true")


@cmd_parse(podman_compose, ["build", "up"])
def compose_build_up_parse(parser):
    parser.add_argument(
        "--pull",
        help="attempt to pull a newer version of the image",
        action="store_true",
    )
    parser.add_argument(
        "--pull-always",
        help="attempt to pull a newer version of the image, Raise an error even if the image is "
        "present locally.",
        action="store_true",
    )
    parser.add_argument(
        "--build-arg",
        metavar="key=val",
        action="append",
        default=[],
        help="Set build-time variables for services.",
    )
    parser.add_argument(
        "--no-cache",
        help="Do not use cache when building the image.",
        action="store_true",
    )


@cmd_parse(podman_compose, ["build", "up", "down", "start", "stop", "restart"])
def compose_build_parse(parser):
    parser.add_argument(
        "services",
        metavar="services",
        nargs="*",
        default=None,
        help="affected services",
    )


@cmd_parse(podman_compose, "config")
def compose_config_parse(parser):
    parser.add_argument(
        "--no-normalize", help="Don't normalize compose model.", action="store_true"
    )
    parser.add_argument(
        "--services", help="Print the service names, one per line.", action="store_true"
    )


@cmd_parse(podman_compose, "port")
def compose_port_parse(parser):
    parser.add_argument(
        "--index",
        type=int,
        default=1,
        help="index of the container if there are multiple instances of a service",
    )
    parser.add_argument(
        "--protocol",
        choices=["tcp", "udp"],
        default="tcp",
        help="tcp or udp",
    )
    parser.add_argument("service", metavar="service", nargs=None, help="service name")
    parser.add_argument(
        "private_port",
        metavar="private_port",
        nargs=None,
        type=int,
        help="private port",
    )


@cmd_parse(podman_compose, ["pause", "unpause"])
def compose_pause_unpause_parse(parser):
    parser.add_argument(
        "services", metavar="services", nargs="*", default=None, help="service names"
    )


@cmd_parse(podman_compose, ["kill"])
def compose_kill_parse(parser):
    parser.add_argument(
        "services", metavar="services", nargs="*", default=None, help="service names"
    )
    parser.add_argument(
        "-s",
        "--signal",
        type=str,
        help="Signal to send to the container (default 'KILL')",
    )
    parser.add_argument(
        "-a",
        "--all",
        help="Signal all running containers",
        action="store_true",
    )


@cmd_parse(podman_compose, "images")
def compose_images_parse(parser):
    parser.add_argument("-q", "--quiet", help="Only display images IDs", action="store_true")


@cmd_parse(podman_compose, ["stats"])
def compose_stats_parse(parser):
    parser.add_argument(
        "services", metavar="services", nargs="*", default=None, help="service names"
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=int,
        help="Time in seconds between stats reports (default 5)",
    )
    parser.add_argument(
        "--no-reset",
        help="Disable resetting the screen between intervals",
        action="store_true",
    )
    parser.add_argument(
        "--no-stream",
        help="Disable streaming stats and only pull the first result",
        action="store_true",
    )


@cmd_parse(podman_compose, ["ps", "stats"])
def compose_format_parse(parser):
    parser.add_argument(
        "-f",
        "--format",
        type=str,
        help="Pretty-print container statistics to JSON or using a Go template",
    )


async def async_main():
    await podman_compose.run()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
