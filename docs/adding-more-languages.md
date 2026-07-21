# Adding C#, Kotlin, or Dart

These are left out of the base `Dockerfile` on purpose — each one adds a JDK
or SDK download in the multi-hundred-MB range, which meaningfully increases
build time and can push you toward needing a paid Space tier for build
resources. Add them once the core setup is confirmed working.

Add the relevant block below into the `Dockerfile`, in the root-level `RUN`
section (before `USER appuser`), then add the matching entry to
`LANGUAGE_CONFIG` in `main.py`.

## C# (via dotnet-script)
```dockerfile
RUN curl -sSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh \
    && chmod +x /tmp/dotnet-install.sh \
    && /tmp/dotnet-install.sh --channel 8.0 --install-dir /usr/share/dotnet \
    && ln -s /usr/share/dotnet/dotnet /usr/bin/dotnet
ENV PATH="${PATH}:/root/.dotnet/tools"
RUN dotnet tool install -g dotnet-script
```
```python
"csharp": {"ext": ".csx", "run": lambda f: ["dotnet-script", f]},
```
Note: dotnet-script's own cache dir also needs to land under `/tmp` at
runtime — set `ENV NUGET_PACKAGES=/tmp/nuget` alongside the other cache vars.

## Kotlin
```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends default-jdk-headless \
    && rm -rf /var/lib/apt/lists/*
RUN curl -s https://get.sdkman.io | bash \
    && bash -c "source /root/.sdkman/bin/sdkman-init.sh && sdk install kotlin"
ENV PATH="${PATH}:/root/.sdkman/candidates/kotlin/current/bin"
```
```python
"kotlin": {"ext": ".kts", "run": lambda f: ["kotlinc", "-script", f]},
```
Kotlin's compiler is JVM-based and slow to start — expect several seconds of
overhead per run even for trivial scripts. Consider a longer per-language
default timeout for this one.

## Dart
```dockerfile
RUN curl -fsSL https://storage.googleapis.com/dart-archive/channels/stable/release/latest/linux_packages/dart_amd64.deb \
      -o /tmp/dart.deb \
    && apt-get update && apt-get install -y /tmp/dart.deb \
    && rm /tmp/dart.deb && rm -rf /var/lib/apt/lists/*
```
```python
"dart": {"ext": ".dart", "run": lambda f: ["dart", f]},
```

After adding any of these, rebuild and check the Space's **Logs** tab — if
the free-tier build times out, that's the signal to either trim languages
back down or move the build to a paid Space tier (Spaces bills by build
minutes/hardware, not per code execution, so this doesn't affect your
per-run costs — only how you deploy).
