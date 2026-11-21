# Skill: Minecraft Fabric Mod Development

Use when building Minecraft Fabric mods (Java 21, Fabric Loader, Fabric API).

## Project Structure
```
src/main/java/com/yourmod/
  YourMod.java          # Main entrypoint (implements ModInitializer)
  mixin/                # ASM mixins
src/main/resources/
  fabric.mod.json       # Mod metadata
  yourmod.mixins.json   # Mixin config
build.gradle            # Loom build config
gradle.properties       # MC version, Fabric versions
```

## Build Commands
```bash
./gradlew build             # Full jar build → build/libs/*.jar
./gradlew runClient         # Launch test client
./gradlew genSources        # Decompile Minecraft sources for reference
```

## Key Conventions
- All mod entrypoints registered in `fabric.mod.json` under `"entrypoints"`
- Events via `ServerTickEvents.END_SERVER_TICK.register(...)` etc.
- Item/Block registration in `Registry.register(Registries.ITEM, ...)`
- Use `FabricItemSettings` not vanilla `Item.Settings` directly
- Mixins target obfuscated names; use `@Shadow` / `@Inject` / `@Redirect`

## Common Errors
- `ClassNotFoundException: net.fabricmc...` → wrong Fabric API version in `gradle.properties`
- Mixin not applying → verify `yourmod.mixins.json` lists the mixin class correctly
