load("@rules_pkg//:pkg.bzl", "pkg_tar")
load("@io_bazel_rules_docker//container:container.bzl", "container_image", "container_layer")
load("@debian_buster_amd64//debs:deb_packages.bzl", packages = "debian_buster_amd64")
load(":caps.bzl", "container_image_setcap")

# Defines a common base image for all app images.
def scion_app_base_gen(name, base_image):
    # Debian packages to install.
    debs = [
        packages["libc6"],
        # we need setcap so that we can add network capabilities to apps
        packages["libcap2"],
        packages["libcap2-bin"],
    ]

    # Environment variables to set.
    env = {"TZ": "UTC"}

    container_layer(
        name = "%s_share_dirs_layer" % name,
        empty_dirs = [
            "/share/cache",
            "/share/data",
            "/share/data/trustdb",
        ],
        mode = "0777",
    )

    # Currently in opensource there are tests (reload_X) that are doing
    # shell commands into the container. We need to change that behavior
    # and once we do that we should only use the prod thin image without
    # shell.
    container_image(
        name = name,
        base = base_image,
        env = env,
        debs = debs,
        tars = [
            "//licenses:licenses",
        ],
        layers = [":%s_share_dirs_layer" % name],
        visibility = ["//visibility:public"],
    )

def scion_app_base():
    scion_app_base_gen("app_base", "@debug_debian10//image")

def scion_app_base_kathara():
    scion_app_base_gen("app_base_kathara", "@debian10//image")
    

# Defines images for a specific SCION application.
# Creates "{name}" targets.
#   name - name of the rule
#   src - the target that builds the app binary
#   appdir - the directory to deploy the binary to
#   workdir - working directory
#   entrypoint - a list of strings that add up to the command line
#   cmd - string or list of strings of commands to execute in the image.
#   caps - capabilities to set on the binary
def scion_app_images_gen(base_image, name, src, entrypoint, appdir = "/app", workdir = "/share", cmd = None, caps = None, caps_binary = None):
    pkg_tar(
        name = "%s_docker_files" % name,
        srcs = [src],
        package_dir = appdir,
        mode = "0755",
    )
    container_image_setcap(
        name = name,
        repository = "scion",
        base = base_image,
        tars = [":%s_docker_files" % name],
        workdir = workdir,
        cmd = cmd,
        entrypoint = entrypoint,
        caps_binary = caps_binary,
        caps = caps,
    )

def scion_app_images(name, src, entrypoint, appdir = "/app", workdir = "/share", cmd = None, caps = None, caps_binary = None):
    scion_app_images_gen("//docker:app_base", name, src, entrypoint, appdir, workdir, cmd, caps, caps_binary)

def scion_app_images_kathara(name, src, entrypoint, appdir = "/app", workdir = "/share", cmd = None, caps = None, caps_binary = None):
    scion_app_images_gen("//docker:app_base_kathara", name, src, entrypoint, appdir, workdir, cmd, caps, caps_binary)