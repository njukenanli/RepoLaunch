from __future__ import annotations

from launch.core.platforms.base import (
    CMD_OUTPUT_PS1_BEGIN,
    CMD_OUTPUT_PS1_END,
    CMD_OUTPUT_METADATA_PS1_REGEX,
    ANSI_ESCAPE,
    TIMEOUT_EXIT_CODE,
    MEM_LIMIT,
    CPU_CORES,
    VAR_PATTERNS
)
from launch.core.platforms.base import (
    CmdOutputMetadata,
    CommandResult
)

from launch.core.platforms.linux import LinuxRuntime

import os
from typing import Any
import queue
import time
import uuid
import shlex

import docker
from docker.models.containers import Container


class MacosRuntime(LinuxRuntime):
    '''
    still run linux images... such as sickcodes/docker-osx:auto, dockurr/macos...
    '''
    image = "sickcodes/docker-osx:auto"
    username = "user"
    password = "alpine"
    swap_mount_tag = "repolaunch_swap"
    wrapper_swap_dir = f"/mnt/{swap_mount_tag}"

    def __init__(
                    self,
                    container: Container,
                    command_timeout: int = 30
                ):
        self.container = container
        self.platform = "macos"
        self.command_timeout = command_timeout
        self.working_dir = "/Users/user/testbed"
        self.mnt_host = os.path.join(os.getcwd(), "tmp")
        self.mnt_container = f"/Volumes/{self.swap_mount_tag}"
        self.sock = self.container.attach_socket(
            params={"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1}
        )
        self.output_queue: queue.Queue[bytes] = queue.Queue()
        self.stopped = False
        self._start_output_thread()
        self._clear_initial_prompt()
        self._wait_until_shell_ready()
        self._mount_swap_directory()
        self.send_command(
            f"mkdir -p {shlex.quote(self.working_dir)}; cd {shlex.quote(self.working_dir)}"
        )

    def _read_until_text(self, text: str, timeout: int = 5) -> str:
        accumulated_output = ""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                chunk = self.output_queue.get(timeout=0.1)
                accumulated_output += chunk.decode("utf-8", errors="ignore")
                if text in ANSI_ESCAPE.sub("", accumulated_output).replace("\r", ""):
                    break
            except queue.Empty:
                continue
        return ANSI_ESCAPE.sub("", accumulated_output).replace("\r", "")

    def _wait_until_shell_ready(self, timeout: int = 300) -> None:
        start_time = time.time()
        while time.time() - start_time < timeout:
            marker = f"REPOLAUNCH_MACOS_READY_{uuid.uuid4().hex}"
            self._clear_initial_prompt()
            self._send_bytes(f"printf '{marker}\\n'\n".encode())
            if marker in self._read_until_text(marker, timeout=5):
                return
            time.sleep(5)
        raise RuntimeError("macOS shell did not become ready before timeout.")

    def _mount_swap_directory(self) -> None:
        password = shlex.quote(self.password)
        tag = shlex.quote(self.swap_mount_tag)
        mnt = shlex.quote(self.mnt_container)
        command = f"""
if ! (test -d {mnt} && test -w {mnt}); then
  printf '%s\\n' {password} | sudo -S mount_9p {tag}
fi
test -d {mnt} && test -w {mnt}
"""
        res = self.send_command(command, timeout=5)
        if int(res.metadata.exit_code) != 0:
            raise RuntimeError(
                f"Failed to mount macOS 9p swap directory {self.mnt_container}: {res.output}"
            )

    def send_command(self, command: str, timeout: int|None = None) -> CommandResult:
        '''
        Run a command in the attached macOS guest shell and append RepoLaunch metadata.
        '''
        timeout = self.command_timeout * 60 if timeout is None else timeout * 60

        if self.stopped:
            raise RuntimeError("container is stopped. Currently we have not enabled container restart after docker commit. If you need to restore the container you must launch from the new image you committed.")

        if not command.endswith("\n"):
            command += "\n"

        metadata_command = r'''
__repolaunch_ec=$?
__repolaunch_py="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
printf '\n###PS1JSON###\n'
printf '{"exit_code":%s,"username":"%s","hostname":"%s","working_dir":"%s","py_interpreter_path":"%s"}\n' "$__repolaunch_ec" "$(whoami)" "$(hostname)" "$(pwd)" "$__repolaunch_py"
printf '###PS1END###\n'
'''

        self._clear_initial_prompt()
        self._send_bytes((command + metadata_command).encode())

        output, metadata = self._read_raw_output(timeout=timeout)
        if metadata is not None:
            return CommandResult(output=output, metadata=metadata)

        for _ in range(10):
            self._send_bytes(b"\x03")

        kill_timeout = 5
        kill_output, kill_metadata = self._read_raw_output(timeout=kill_timeout)

        output = output + kill_output + "\n**Exited due to timeout**\n"
        if kill_metadata is not None:
            kill_metadata.exit_code = TIMEOUT_EXIT_CODE
            return CommandResult(output=output, metadata=kill_metadata)

        fallback_metadata = CmdOutputMetadata(exit_code=TIMEOUT_EXIT_CODE)
        return CommandResult(output=output, metadata=fallback_metadata)

    @classmethod
    def _start_container(
        cls,
        image_name: str,
        container_id: str,
        docker_timeout: int,
        command_timeout: int,
    ) -> MacosRuntime:
        if not os.path.exists("/dev/kvm"):
            raise RuntimeError("MacosRuntime requires a Linux/WSL host with /dev/kvm available.")
        try:
            docker.from_env().ping()
        except docker.errors.DockerException:
            raise RuntimeError("Docker is not installed or not running.")

        _ = cls.pull_image(image_name)
        client = docker.from_env(timeout=docker_timeout)
        container_name = f"git-launch-{container_id}-{str(uuid.uuid4())[:4]}"
        info = client.version()
        engine_os = (info.get("Os") or info.get("OSType") or "").lower()
        if "linux" not in engine_os:
            raise RuntimeError("MacosRuntime requires Docker to run Linux containers with KVM support.")
        extra_hosts = {"host.docker.internal": "host-gateway"}

        os.makedirs(os.path.join(os.getcwd(), "tmp"), exist_ok=True)
        run_kwargs = {
            "cpu_quota": int(CPU_CORES * 100000),
            "mem_limit": MEM_LIMIT,
            "devices": ["/dev/kvm:/dev/kvm"],
        }
        environment = {
            "TERM": "xterm-mono",
            "NOPICKER": "true",
            "USERNAME": cls.username,
            "PASSWORD": cls.password,
            "EXTRA": (
                f"-virtfs local,path={cls.wrapper_swap_dir},"
                f"mount_tag={cls.swap_mount_tag},"
                f"security_model=passthrough,id={cls.swap_mount_tag}"
            ),
        }

        container = client.containers.run(
            image_name,
            name=container_name,
            stdin_open=True,
            tty=True,
            detach=True,
            environment=environment,
            extra_hosts=extra_hosts,
            ports={"10022/tcp": None},
            volumes={
                os.path.join(os.getcwd(), "tmp"): {
                    "bind": cls.wrapper_swap_dir,
                    "mode": "rw",
                }
            },
            **run_kwargs,
        )

        session = cls(
                    container,
                    command_timeout=command_timeout,
                )

        return session

    @classmethod
    def start_runtime_from_launch_image(
        cls,
        image_name: str,
        instance_id: str,
        command_timeout: int = 30,
    ) -> MacosRuntime:
        container_id = instance_id.replace("/", "_")
        return cls._start_container(
            image_name=image_name,
            container_id=container_id,
            docker_timeout=7200,
            command_timeout=command_timeout,
        )

    @classmethod
    def start_runtime_from_base_image(
        cls,
        image_name: str,
        instance: dict[str, Any],
        command_timeout: int = 30,
    ) -> MacosRuntime:
        container_id = instance["instance_id"].replace("/", "_")
        session = cls._start_container(
            image_name=image_name,
            container_id=container_id,
            docker_timeout=18000,
            command_timeout=command_timeout,
        )

        url = f'https://github.com/{instance["repo"]}.git'
        base_commit = instance["base_commit"]
        working_dir = shlex.quote(session.working_dir)

        session.send_command("git --version || true")
        res: CommandResult = session.send_command(
            f"git config --global --add safe.directory {working_dir}; "
            f"mkdir -p {working_dir}; "
            f"git init {working_dir}; "
            f"cd {working_dir}; "
            f"git remote add origin {shlex.quote(url)}; "
            f"git fetch --depth 1 origin {shlex.quote(base_commit)}; "
            f"git reset --hard {shlex.quote(base_commit)}"
        )

        session.send_command("ls")

        if int(res.metadata.exit_code) != 0:
            session.cleanup()
            raise RuntimeError(f"Git clone/reset failed: \n{res.output}")

        return session

    def commit(self, image_name: str, tag: str = "latest", push: bool = False) -> str:
        try:
            self.send_command("sync", timeout=5)
            self._send_bytes(
                f"printf '%s\\n' {shlex.quote(self.password)} | sudo -S shutdown -h now\n".encode()
            )
            time.sleep(20)
        except Exception as e:
            print(f"Failed to gracefully shut down macOS guest before commit: {e}")

        self.container.stop(timeout=120)

        self.container.commit(
            repository=image_name,
            tag=tag,
        )
        print(f"Image {image_name}:{tag} created successfully.")

        if push:
            client = docker.from_env()
            client.images.push(image_name, tag=tag)
            print(f"Image {image_name}:{tag} pushed successfully.")

        self.cleanup()
        return f"{image_name}:{tag}"
