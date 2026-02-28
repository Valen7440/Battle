# TODO: Credits to [Caylies](https://github.com/Caylies) for the original code!
import base64
import aiohttp
import os
from packaging.version import parse as parse_version

from ballsdex import __version__

SUPPORTED_VERSION_AT = "2.29.3"

if parse_version(__version__) < parse_version(SUPPORTED_VERSION_AT):
    raise Exception(
        f"Unsupported ballsdex version (Actual Version: {__version__}) "
        f"Version > 2.29.3 is required."
    )

GITHUB = "Valen7440/Battle/contents/"
PACKAGE_PATH = "ballsdex/packages/battle"
PACKAGE_FILES = [
    "__init__.py", 
    "cog.py",
    "display.py",
    "menu.py",
    "types.py"
]

os.makedirs("ballsdex/packages/collectible", exist_ok=True)

async def fetch_github_file(session: aiohttp.ClientSession, url: str):
    async with session.get(url) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Failed to fetch {url} ({resp.status})")
        data = await resp.json()
        return data

async def add_package(package: str):
    """
    Adds a package to the config.yml file.

    Parameters
    ----------
    package: str
        The package you want to append to the config.yml file.
    """
    with open("config.yml", "r") as file:
        lines = file.readlines()

    item = f"  - {package}\n"

    if "packages:\n" not in lines or item in lines:
        return

    for i, line in enumerate(lines):
        if line.rstrip().startswith("packages:"):
            lines.insert(i + 1, item)
            break

    with open("config.yml", "w") as file:
        file.writelines(lines)

    await ctx.send("Added package to config file")

async def install_package_files():
    """
    Installs and updates files from the GitHub page.
    """
    files = PACKAGE_FILES.copy()
    progress_message = await ctx.send(
        f"Installing package files: 0% (0/{len(files)})"
    )

    log = []
    async with aiohttp.ClientSession() as session:
        for index, file in enumerate(files):
            data = await fetch_github_file(session, f"https://api.github.com/repos/{GITHUB}/battle/{file}")

            remote_content = base64.b64decode(data["content"]).decode("UTF-8")
            local_file_path = f"{PACKAGE_PATH}/{file}"

            with open(local_file_path, "w") as opened_file:
                opened_file.write(remote_content)

            log.append(f"-# Installed `{file}`")

            percentage = round((index + 1) / len(files) * 100)

            await progress_message.edit(
                content=(
                    f"Installing package files: {percentage}% ({index + 1}/{len(files)})"
                    f"\n{'\n'.join(log)}"
                )
            )

            await asyncio.sleep(1)

await install_package_files()
await add_package(PACKAGE_PATH.replace("/", "."))

await ctx.send("Reloading commands...")

try:
    await bot.reload_extension(PACKAGE_PATH.replace("/", "."))
except commands.ExtensionNotLoaded:
    await bot.load_extension(PACKAGE_PATH.replace("/", "."))

await bot.tree.sync()

await ctx.send("Finished installing/updating everything!")