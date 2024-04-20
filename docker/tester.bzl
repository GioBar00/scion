load("@rules_pkg//:pkg.bzl", "pkg_tar")
load("@io_bazel_rules_docker//container:container.bzl", "container_image")
load("@io_bazel_rules_docker//docker/package_managers:download_pkgs.bzl", "download_pkgs")
load("@io_bazel_rules_docker//docker/package_managers:install_pkgs.bzl", "install_pkgs")
load("@debian_buster_amd64//debs:deb_packages.bzl", packages = "debian_buster_amd64")

def build_tester_image_gen(name, base_image, layers = [], tars = []):
    download_pkgs(
        name = "%s_pkgs" % name,
        image_tar = base_image,
        packages = [
            "bridge-utils",
            "iperf3",
            "iptables",
            "netcat-openbsd",
            "openssh-server",
            "openssh-client",
            "procps",
            "telnet",
            "tshark",
            "wget",
        ],
    )

    install_pkgs(
        name = "%s_pkgs_image" % name,
        image_tar = base_image,
        installables_tar = ":%s_pkgs.tar" % name,
        installation_cleanup_commands = "rm -rf /var/lib/apt/lists/*",
        output_image_name = "%s_pkgs_image" % name,
    )

    pkg_tar(
        name = "%s_bin" % name,
        srcs = [
            "//tools/end2end:end2end",
            "//tools/end2endblast:end2endblast",
            "//scion/cmd/scion",
            "//scion-pki/cmd/scion-pki:scion-pki",
        ],
        package_dir = "bin",
    )

    pkg_tar(
        name = "%s_integration" % name,
        srcs = [
            "//tools/integration:bin_wrapper.sh",
        ],
        package_dir = "tools/integration",
    )

    pkg_tar(
        name = "%s_share" % name,
        deps = [
            ":%s_bin" % name,
            ":%s_integration" % name,
        ],
        srcs = [":tester_files"],
        package_dir = "share",
    )

    container_image(
        name = name,
        base = ":%s_pkgs_image.tar" % name,
        env = {
            "TZ": "UTC",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/share/bin",
        },
        tars = tars,
        workdir = "/share",
        cmd = "tail -f /dev/null",
        layers = layers,
        visibility = ["//visibility:public"],
    )

def build_tester_image():
    build_tester_image_gen("tester", "@debian10//image", tars = [":tester_share"])

def build_endhost_kathara_image():
    pkg_tar(
        name = "endhost_kathara_docker_files",
        srcs = ["//daemon/cmd/daemon"],
        package_dir = "/app",
        mode = "0755",
    )
    build_tester_image_gen("endhost_kathara", "@debian10//image", [":app_base_kathara_share_dirs_layer"], ["endhost_kathara_share", "endhost_kathara_docker_files"])
    