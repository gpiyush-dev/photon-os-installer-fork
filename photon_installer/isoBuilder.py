#!/usr/bin/env python3

import os
import glob
import json
import tempfile
import shutil
import platform
import yaml

from logger import Logger
from argparse import ArgumentParser
from commandutils import CommandUtils
from tdnf import Tdnf


class IsoBuilder(object):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.pkg_list = []
        self.working_dir = tempfile.mkdtemp(
            prefix="photon-", dir=self.artifact_path
        )
        self.iso_name = os.path.join(
            self.artifact_path, f"photon-{self.photon_release_version}.iso"
        )
        self.rpms_path = os.path.join(self.working_dir, "RPMS")
        self.initrd_path = os.path.join(self.working_dir, "photon-chroot")
        self.photon_docker_image = f"photon:{self.photon_release_version}"
        self.logger = Logger.get_logger(
            os.path.join(self.artifact_path, "LOGS"), self.log_level, True
        )
        self.cmdUtil = CommandUtils(self.logger)
        self.architecture = platform.machine()
        self.additional_files = []
        self.repos_dir = os.path.join(self.working_dir, "yum.repos.d")

        self.tdnf = Tdnf(
            logger=self.logger,
            releasever=self.photon_release_version,
            reposdir=self.repos_dir,
            docker_image=self.photon_docker_image,
        )

    def runCmd(self, cmd):
        retval = self.cmdUtil.run(cmd)
        if retval:
            raise Exception(f"Following command failed to execute: {cmd}")

    def addPkgsToList(self, pkg_list_file):
        if os.path.exists(pkg_list_file):
            pkg_data = CommandUtils.jsonread(pkg_list_file)
            self.pkg_list.extend(pkg_data["packages"])
            if f"packages_{self.architecture}" in pkg_data:
                self.pkg_list.extend(pkg_data[f"packages_{self.architecture}"])

    def addGrubConfig(self):
        self.logger.info("Adding grub config...")
        if not os.path.exists(f"{self.working_dir}/boot/grub2"):
            self.logger.info(
                f"Creating grub dir: {self.working_dir}/boot/grub2"
            )
            os.makedirs(f"{self.working_dir}/boot/grub2")
        with open(f"{self.working_dir}/boot/grub2/grub.cfg", "w") as conf_file:
            conf_file.writelines(
                [
                    "set default=0\n",
                    "set timeout=3\n",
                    "loadfont ascii\n",
                    'set gfxmode="1024x768"\n',
                    "gfxpayload=keep\n",
                    "set theme=/boot/grub2/themes/photon/theme.txt\n",
                    "terminal_output gfxterm\n",
                    "probe -s photondisk -u ($root)\n\n",
                    'menuentry "Install" {\n',
                    f"linux /isolinux/vmlinuz root=/dev/ram0 loglevel=3 photon.media=UUID=$photondisk {self.boot_cmdline_param}\n",
                    "initrd /isolinux/initrd.img\n}",
                ]
            )

    def createInstallOptionJson(self):
        install_option_key = "custom"
        additional_files = [
            os.path.basename(file) for file in self.additional_files
        ]
        if self.function == "build-rpm-ostree-iso":
            install_option_key = "ostree_host"
            additional_files = ["ostree-repo.tar.gz"]
        install_option_data = {
            install_option_key: {
                "title": "Photon Custom",
                "packagelist_file": os.path.basename(self.packageslist_file),
                "visible": False,
                "additional-files": additional_files,
            }
        }
        with open(
            f"{self.working_dir}/build_install_options_custom.json", "w"
        ) as json_file:
            json_file.write(json.dumps(install_option_data))

    def setupReposDir(self):
        os.makedirs(self.repos_dir, exist_ok=True)

        # copy repo files from host
        if os.path.isdir("/etc/yum.repos.d"):
            for repo_file in glob.glob("/etc/yum.repos.d/*.repo"):
                shutil.copy(repo_file, self.repos_dir)

        # additional repos
        if self.additional_repos:
            for repo_file in self.additional_repos:
                shutil.copy(repo_file, self.repos_dir)

    def generateInitrd(self):
        """
        Generate custom initrd
        """
        initrd_pkgs = None
        if not os.path.exists(self.working_dir):
            self.logger.info(f"Creating working directory: {self.working_dir}")
            os.makedirs(self.working_dir)
        if not self.initrd_pkg_list_file:
            initrd_pkg_file = f"https://raw.githubusercontent.com/vmware/photon/{self.photon_release_version}/common/data/packages_installer_initrd.json"
            self.logger.info(
                f"Downloading initrd package list file {initrd_pkg_file}..."
            )
            self.cmdUtil.wget(
                initrd_pkg_file,
                f"{self.working_dir}/packages_installer_initrd.json",
            )
            self.initrd_pkg_list_file = (
                f"{self.working_dir}/packages_installer_initrd.json"
            )
        initrd_pkg_data = CommandUtils.jsonread(self.initrd_pkg_list_file)
        initrd_pkgs = initrd_pkg_data["packages"]
        if f"packages_{self.architecture}" in initrd_pkg_data:
            initrd_pkgs.extend(
                initrd_pkg_data[f"packages_{self.architecture}"]
            )
        self.logger.info(f"Initrd package list: {initrd_pkgs}")
        initrd_pkgs = " ".join(initrd_pkgs)

        # Download all initrd packages before installing them during initrd generation.
        # Skip downloading packages if ostree iso.
        ostree_iso = False
        if self.function != "build-rpm-ostree-iso":
            self.setupReposDir()
            self.downloadPkgs()
        else:
            ostree_iso = True
            self.additional_files.append(self.ostree_tar_path)

        self.createInstallOptionJson()

        # Get absolute path of generate_initrd script
        initrd_script = (
            f"{os.path.dirname(os.path.abspath(__file__))}/generate_initrd.sh"
        )
        self.logger.info("Starting to generate initrd.img...")
        self.runCmd(
            [
                initrd_script,
                self.working_dir,
                initrd_pkgs,
                self.rpms_path,
                self.photon_release_version,
                self.packageslist_file,
                "build_install_options_custom.json",
                str(ostree_iso),
            ]
        )

    def downloadRequiredFiles(self):
        """
        Download required files to generate specific image in working directory.

        ISO's: [open_source_license.tar.gz, sample_ks.cfg, sample_ui.cfg, NOTICE-Apachev2, NOTICE-GPL2.0, EULA.txt]
        initrd: [sample_ks.cfg, sample_ui.cfg, EULA.txt]
        """

        if not os.path.exists(self.working_dir):
            self.logger.info(f"Creating working directory: {self.working_dir}")
            os.makedirs(self.working_dir)

        # Download required files for given branch and extract it in working dir.
        files_to_download = [
            f"https://raw.githubusercontent.com/vmware/photon/{self.photon_release_version}/support/image-builder/iso/sample_ks.cfg",
            f"https://raw.githubusercontent.com/vmware/photon/{self.photon_release_version}/support/image-builder/iso/sample_ui.cfg",
            f"https://raw.githubusercontent.com/vmware/photon/{self.photon_release_version}/EULA.txt",
        ]

        if "iso" in self.function:
            files_to_download.extend(
                [
                    f"https://raw.githubusercontent.com/vmware/photon/{self.photon_release_version}/NOTICE-Apachev2",
                    f"https://raw.githubusercontent.com/vmware/photon/{self.photon_release_version}/NOTICE-GPL2.0",
                    f"https://github.com/vmware/photon/raw/{self.photon_release_version}/support/image-builder/iso/open_source_license.tar.gz",
                ]
            )

        # Download ostree tar to working directory if url is provided.
        if (
            self.function == "build-rpm-ostree-iso"
            and self.ostree_tar_path.startswith("http")
        ):
            files_to_download.append(self.ostree_tar_path)
            self.ostree_tar_path = (
                f"{self.working_dir}/{os.path.basename(self.ostree_tar_path)}"
            )

        for file in files_to_download:
            self.logger.info(f"Downloading file: {file}")
            secure_download = True
            output_file = os.path.basename(file)
            if file.startswith("http:"):
                secure_download = False
            if self.ostree_tar_path and os.path.basename(
                file
            ) == os.path.basename(self.ostree_tar_path):
                output_file = "ostree-repo.tar.gz"
            retval, msg = self.cmdUtil.wget(
                file,
                f'{self.working_dir}/{output_file}',
                enforce_https=secure_download,
            )
            if not retval:
                raise Exception(msg)

    def downloadPkgs(self):
        if not os.path.exists(self.rpms_path):
            self.logger.info(f"Creating RPMS directory: {self.rpms_path}")
            os.makedirs(self.rpms_path, exist_ok=True)

        # Add installer initrd and custom packages to package list..
        self.addPkgsToList(self.initrd_pkg_list_file)
        self.addPkgsToList(self.packageslist_file)

        linux_flavors = [
            "linux",
            "linux-esx",
            "linux-rt",
            "linux-aws",
            "linux-secure",
        ]
        if not any(flavor in self.pkg_list for flavor in linux_flavors):
            self.pkg_list.append("linux")

        # Include additional packages if mentioned in kickstart.
        if self.kickstart_path:
            kickstart_data = CommandUtils.jsonread(self.kickstart_path)
            if "packages" in kickstart_data:
                self.pkg_list.extend(kickstart_data["packages"])

        pkg_list = " ".join(self.pkg_list)
        self.logger.info(f"List of packages to download: {pkg_list}")

        # skip downloading if repo already exists
        if not os.path.isdir(os.path.join(self.rpms_path, 'repodata')):
            self.logger.info("downloading packages...")
            retval, tdnf_out = self.tdnf.run(
                [
                    '--alldeps',
                    '--downloadonly',
                    '--downloaddir',
                    self.rpms_path,
                    'install',
                ]
                + self.pkg_list,
                directories=[self.rpms_path],
            )
            if retval != 0:
                raise Exception(f"tdnf failed: {tdnf_out}")
            self.logger.info("...done.")

        # Separate out packages downloaded into arch specific directories.
        # Run createrepo on the rpm download path once downloaded.
        if not os.path.exists(f"{self.rpms_path}/x86_64"):
            os.mkdir(f"{self.rpms_path}/x86_64")
        if not os.path.exists(f"{self.rpms_path}/noarch"):
            os.mkdir(f"{self.rpms_path}/noarch")
        for file in os.listdir(f"{self.working_dir}/RPMS"):
            if file.endswith('.x86_64.rpm'):
                shutil.move(
                    f"{self.rpms_path}/{file}",
                    f"{self.rpms_path}/x86_64/{file}",
                )
            elif file.endswith('.noarch.rpm'):
                shutil.move(
                    f"{self.rpms_path}/{file}",
                    f"{self.rpms_path}/noarch/{file}",
                )
        self.logger.info("Creating repodata for downloaded packages...")
        self.createRepo()

    def createRepo(self):
        repoDataDir = f"{self.rpms_path}/repodata"
        self.runCmd(f"createrepo --database --update {self.rpms_path}")
        if os.path.exists(repoDataDir):
            primary_xml_gz = glob.glob(repoDataDir + "/*primary.xml.gz")
            self.runCmd(
                f"ln -sfv {primary_xml_gz[0]} {repoDataDir}/primary.xml.gz"
            )

    def cleanUp(self):
        try:
            shutil.rmtree(self.working_dir)
        except FileNotFoundError:
            pass

    def createEfiImg(self):
        """
        create efi image
        """
        self.logger.info("Creating EFI image...")
        self.efi_img = "boot/grub2/efiboot.img"
        efi_dir = os.path.join(self.artifact_path, "efiboot")
        self.runCmd(
            f"dd if=/dev/zero of={self.working_dir}/{self.efi_img} bs=3K count=1024"
        )
        self.runCmd(f"mkdosfs {self.working_dir}/{self.efi_img}")
        os.makedirs(efi_dir)
        self.runCmd(
            f"mount -o loop {self.working_dir}/{self.efi_img} {efi_dir}"
        )
        shutil.move(f"{self.working_dir}/boot/efi/EFI", efi_dir)
        os.listdir(efi_dir)
        self.runCmd(f"umount {efi_dir}")
        shutil.rmtree(efi_dir)

    def createIsolinux(self):
        """
        Install photon-iso-config rpm in working dir.
        """
        if not os.path.exists(f"{self.working_dir}/isolinux"):
            os.makedirs(f"{self.working_dir}/isolinux")
        shutil.move(
            f"{self.working_dir}/initrd.img", f"{self.working_dir}/isolinux"
        )

        self.logger.info(
            "Installing photon-iso-config and syslinux in working directory..."
        )
        os.makedirs(f"{self.working_dir}/isolinux-temp")
        pkg_list = ["photon-iso-config"]
        if self.architecture == "x86_64":
            pkg_list.extend(["syslinux"])

        self.logger.info("installing packages for isolinux...")
        isolinux_dir = os.path.join(self.working_dir, "isolinux-temp")
        retval, tdnf_out = self.tdnf.run(
            ['install', '--installroot', isolinux_dir] + pkg_list,
            directories=[isolinux_dir],
        )
        if retval != 0:
            raise Exception(f"tdnf failed: {tdnf_out}")
        self.logger.info("...done.")

        self.logger.debug(
            "Succesfully installed photon-iso-config syslinux..."
        )
        for file in os.listdir(
            f"{self.working_dir}/isolinux-temp/usr/share/photon-iso-config"
        ):
            shutil.copyfile(
                f"{self.working_dir}/isolinux-temp/usr/share/photon-iso-config/{file}",
                f"{self.working_dir}/isolinux/{file}",
            )
        for file in [
            "isolinux.bin",
            "libcom32.c32",
            "libutil.c32",
            "vesamenu.c32",
            "ldlinux.c32",
        ]:
            shutil.copyfile(
                f"{self.working_dir}/isolinux-temp/usr/share/syslinux/{file}",
                f"{self.working_dir}/isolinux/{file}",
            )
        shutil.rmtree(f"{self.working_dir}/isolinux-temp")
        for file in ["tdnf.conf", "photon-local.repo"]:
            if os.path.exists(f"{self.working_dir}/{file}"):
                os.remove(f"{self.working_dir}/{file}")
        shutil.move(
            f"{self.working_dir}/sample_ks.cfg", f"{self.working_dir}/isolinux"
        )
        if self.kickstart_path:
            self.logger.info(
                f"Moving {self.kickstart_path} to {self.working_dir}/isolinux..."
            )
            shutil.copyfile(
                f"{self.kickstart_path}", f"{self.working_dir}/isolinux"
            )
        if self.boot_cmdline_param:
            self.logger.info(
                "Adding Boot command line paramters to isolinux menu..."
            )
            self.runCmd(
                f"sed -i '/photon.media=cdrom/ s#$# {self.boot_cmdline_param}#' {self.working_dir}/isolinux/menu.cfg"
            )

    def copyAdditionalFiles(self):
        for file in self.additional_files:
            output_file = f"{self.working_dir}/{os.path.basename(file)}"
            # Rename ostree tar to ostree-repo.tar.gz in iso.
            if (
                self.ostree_tar_path
                and os.path.basename(file)
                == os.path.basename(self.ostree_tar_path)
                and os.path.basename(self.ostree_tar_path)
                != "ostree-repo.tar.gz"
            ):
                output_file = f"{self.working_dir}/ostree-repo.tar.gz"
            if not os.path.exists(output_file):
                shutil.copy(file, output_file)

    def build(self):
        """
        Create Custom Iso
        """
        if not os.path.exists(self.working_dir):
            self.logger.info(f"Creating working directory: {self.working_dir}")
            os.makedirs(self.working_dir)

        # Create isolinux dir inside iso.
        self.createIsolinux()

        # Copy Additional Files
        # In case of ostree-iso copy ostree tar to working directory.
        self.copyAdditionalFiles()

        self.createEfiImg()
        self.runCmd(
            f"mv {self.working_dir}/boot/vmlinuz* {self.working_dir}/isolinux/vmlinuz"
        )

        # ID in the initrd.gz now is PHOTON_VMWARE_CD . This is how we recognize that the cd is actually ours. touch this file there.
        self.runCmd(f"touch {self.working_dir}/PHOTON_VMWARE_CD")

        self.addGrubConfig()

        self.logger.info(f"Generating Iso: {self.iso_name}")
        build_iso_cmd = f"cd {self.working_dir} && "
        build_iso_cmd += "mkisofs -R -l -L -D -b isolinux/isolinux.bin -c isolinux/boot.cat "
        build_iso_cmd += "-no-emul-boot -boot-load-size 4 -boot-info-table "
        build_iso_cmd += f"-eltorito-alt-boot -e {self.efi_img} -no-emul-boot "
        build_iso_cmd += f"-V \"PHOTON_$(date +%Y%m%d)\" {self.working_dir} > {self.iso_name}"
        self.runCmd(build_iso_cmd)

    def create_file(path, file_name):
        if not os.path.exists(path):
            os.makedirs(path)
        return os.path.join(path, file_name)

    def merge_packages_list(merged_file, file1, file2):
        if file1:
            merged_file = CommandUtils.merge_json_files(
                merged_file, file1, file2
            )
            return merged_file
        else:
            return file2

    @staticmethod
    def validate_options(options, initrd_path, logger):

        if not options.photon_release_version:
            raise Exception(
                "the following arguments are required: -v/--photon-release-version"
            )

        if options.function == "build-rpm-ostree-iso":
            if not options.ostree_tar_path:
                raise Exception("Ostree tar path not provided...")
            elif not options.ostree_tar_path.startswith("http"):
                options.ostree_tar_path = os.path.abspath(
                    options.ostree_tar_path
                )
            if not options.packageslist_file:
                options.packageslist_file = (
                    f"{os.path.dirname(__file__)}/packages_ostree_host.json"
                )

        if isinstance(options.packages_list, dict):
            file_path = IsoBuilder.create_file(
                initrd_path, "custom_pkg_list.json"
            )
            options.packages_list = CommandUtils.write_pkg_list_file(
                file_path, options.packages_list
            )

            merged_file_path = IsoBuilder.create_file(
                initrd_path, "merged_pkgs.json"
            )
            options.packageslist_file = IsoBuilder.merge_packages_list(
                merged_file_path,
                options.packageslist_file,
                options.packages_list,
            )

        elif not os.path.exists(options.packageslist_file):
            raise Exception("Custom packages json doesn't exist.")

        if isinstance(options.initrd_pkgs, dict):
            file_path = IsoBuilder.create_file(
                initrd_path, "custom_initrd_pkgs.json"
            )
            options.initrd_pkgs = CommandUtils.write_pkg_list_file(
                file_path, options.initrd_pkgs
            )

            merged_file_path = IsoBuilder.create_file(
                initrd_path, "merged_initrd_pkgs.json"
            )
            options.initrd_pkgs_list_file = IsoBuilder.merge_packages_list(
                merged_file_path,
                options.initrd_pkgs_list_file,
                options.initrd_pkgs,
            )

        elif not options.initrd_pkgs_list_file:
            logger.warning(
                "WARNING: 'custom-initrd-pkgs' is empty. It will be downloaded from https://raw.githubusercontent.com/vmware/photon/{options.photon_release_version}/common/data/packages_installer_initrd.json"
            )


def main():
    usage = "Usage: %prog [options]"
    parser = ArgumentParser(usage)
    parser.add_argument("-l", "--log-level", dest="log_level", default="info")
    parser.add_argument(
        "-f",
        "--function",
        dest="function",
        default="",
        help="<Required> Building Options",
        choices=["build-iso", "build-initrd", "build-rpm-ostree-iso"],
    )
    parser.add_argument(
        "-v",
        "--photon-release-version",
        dest="photon_release_version",
        default=None,
        help="<Required> Photon release version to build custom iso/initrd.",
    )
    parser.add_argument(
        "-o",
        "--ostree-tar-path",
        dest="ostree_tar_path",
        default="",
        help="Path to custom ostree tar.",
    )
    parser.add_argument(
        "-c",
        "--initrd-pkgs-list-file",
        dest="initrd_pkgs_list_file",
        default=None,
        help="<Optional> parameter to provide cutom initrd pkg list file.",
    )
    parser.add_argument(
        "-i",
        "--initrd-pkgs",
        dest="initrd_pkgs",
        default=None,
        help="<Optional> parameter to provide cutom initrd pkg list",
    )
    parser.add_argument(
        "-r",
        "--additional_repos",
        action="append",
        default=None,
        help="<Optional> Pass repo file as input to download rpms from external repo",
    )
    parser.add_argument(
        "-p",
        "--packageslist-file",
        dest="packageslist_file",
        default="",
        help="Custom package list file.",
    )
    parser.add_argument(
        "-q",
        "--packages",
        dest="packages_list",
        default="",
        help="Custom package list.",
    )
    parser.add_argument(
        "-k",
        "--kickstart-path",
        dest="kickstart_path",
        default=None,
        help="<Optional> Path to custom kickstart file.",
    )
    parser.add_argument(
        "-b",
        "--boot-cmdline-param",
        dest="boot_cmdline_param",
        default="",
        help="<Optional> Extra boot commandline parameter to pass.",
    )
    parser.add_argument(
        "-a",
        "--artifact-path",
        dest="artifact_path",
        default=os.getcwd(),
        help="<Optional> Path to generate iso in.",
    )

    parser.add_argument(
        "-P",
        "--param",
        dest='params',
        action='append',
        default=[],
        help="Specify a parameter value. This option can be used multiple times to provide multiple parameter values.",
    )
    parser.add_argument(
        '-y',
        '--config',
        dest='config',
        type=str,
        help='Path to the configuration YAML file',
        default="",
    )

    # Parse the command-line arguments
    options = parser.parse_args()
    if os.path.exists(options.config):
        params = {}
        for p in options.params:
            k, v = p.split('=')
            params[k] = yaml.safe_load(v)

        # Load config from YAML file
        with open(options.config, 'r') as f:
            config = CommandUtils.readConfig(f, params=params)
            # Override YAML values with command-line arguments
            for dest, value in vars(options).items():
                if value and dest in config:
                    config[dest] = value
        # Add config arguments to options
        options.__dict__.update(config)

    isoBuilder = IsoBuilder(
        function=options.function,
        packageslist_file=options.packageslist_file,
        kickstart_path=options.kickstart_path,
        photon_release_version=options.photon_release_version,
        log_level=options.log_level,
        initrd_pkg_list_file=options.initrd_pkgs_list_file,
        initrd_pkgs=options.initrd_pkgs,
        ostree_tar_path=options.ostree_tar_path,
        additional_repos=options.additional_repos,
        boot_cmdline_param=options.boot_cmdline_param,
        artifact_path=options.artifact_path,
        packages_list=options.packages_list,
    )

    IsoBuilder.validate_options(
        options, f"{isoBuilder.initrd_path}/installer", isoBuilder.logger
    )
    isoBuilder.logger.info(
        f"Starting to generate photon {isoBuilder.photon_release_version} initrd.img..."
    )
    isoBuilder.downloadRequiredFiles()
    isoBuilder.generateInitrd()

    if options.function in ["build-iso", "build-rpm-ostree-iso"]:
        isoBuilder.logger.info(
            f"Starting to generate photon {isoBuilder.photon_release_version} iso..."
        )
        isoBuilder.build()
    elif options.function == "build-initrd":
        isoBuilder.logger.debug(
            f"Moving {isoBuilder.working_dir}/initrd.img to {options.artifact_path}"
        )
        shutil.move(
            f"{isoBuilder.working_dir}/initrd.img", options.artifact_path
        )
    else:
        raise Exception(f"{options.function} not supported...")

    isoBuilder.cleanUp()


if __name__ == '__main__':
    main()
