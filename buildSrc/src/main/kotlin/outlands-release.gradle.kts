/*
 * Minecraft Development for IntelliJ
 *
 * https://mcdev.io/
 *
 * Copyright (C) 2026 minecraft-dev
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Lesser General Public License as published
 * by the Free Software Foundation, version 3.0 only.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

import com.google.gson.GsonBuilder
import javax.xml.parsers.DocumentBuilderFactory
import org.gradle.api.plugins.JavaPluginExtension

plugins {
    id("org.jetbrains.intellij.platform")
    `maven-publish`
}

val releaseMetadata = layout.buildDirectory.file("outlands/release.json")
val patchedDescriptor = tasks.patchPluginXml.flatMap { it.outputFile }
val releaseBranch = providers.gradleProperty("outlands.releaseBranch")
val releaseVersion = providers.gradleProperty("outlands.releaseVersion")
val ideaLine = providers.gradleProperty("ideaVersionName")
val coreVersion = providers.gradleProperty("coreVersion")
val toolchainVersion = extensions.getByType<JavaPluginExtension>().toolchain.languageVersion
val currentVersion = provider { project.version.toString() }
val pluginArchiveName = currentVersion.map { "minecraft-dev-$it.zip" }

tasks.buildPlugin {
    archiveFileName = pluginArchiveName
}

val writeReleaseMetadata by tasks.registering {
    group = "publishing"
    dependsOn(tasks.patchPluginXml)
    inputs.file(patchedDescriptor)
    inputs.property("ideaLine", ideaLine)
    inputs.property("coreVersion", coreVersion)
    inputs.property("jdk", toolchainVersion.map { it.asInt() })
    inputs.property("archive", pluginArchiveName)
    outputs.file(releaseMetadata)

    doLast {
        val document = DocumentBuilderFactory.newInstance().newDocumentBuilder()
            .parse(patchedDescriptor.get().asFile)
        val root = document.documentElement
        val range = root.getElementsByTagName("idea-version").item(0).attributes
        val metadata = linkedMapOf(
            "id" to root.getElementsByTagName("id").item(0).textContent,
            "name" to root.getElementsByTagName("name").item(0).textContent,
            "version" to root.getElementsByTagName("version").item(0).textContent,
            "coreVersion" to coreVersion.get(),
            "ideaLine" to ideaLine.get(),
            "sinceBuild" to range.getNamedItem("since-build").nodeValue,
            "untilBuild" to range.getNamedItem("until-build").nodeValue,
            "jdk" to toolchainVersion.get().asInt(),
            "archive" to pluginArchiveName.get(),
        )
        releaseMetadata.get().asFile.apply {
            parentFile.mkdirs()
            writeText(GsonBuilder().setPrettyPrinting().create().toJson(metadata) + "\n")
        }
    }
}

tasks.register("verifyReleaseMetadata") {
    group = "verification"
    dependsOn(writeReleaseMetadata)
    inputs.file(releaseMetadata)
    inputs.property("releaseBranch", releaseBranch)
    inputs.property("releaseVersion", releaseVersion)

    doLast {
        val line = ideaLine.get()
        check(releaseBranch.get() == line) { "Release branch does not match ideaVersionName=$line" }
        check(releaseVersion.get() == coreVersion.get()) { "Release version does not match coreVersion" }
        check(currentVersion.get() == "$line-${releaseVersion.get()}") {
            "Only an unsuffixed CI release build can be published: ${currentVersion.get()}"
        }
        val parts = line.split('.').map(String::toInt)
        val baseline = (parts[0] % 100) * 10 + parts[1]
        val descriptor = DocumentBuilderFactory.newInstance().newDocumentBuilder()
            .parse(patchedDescriptor.get().asFile).documentElement
        val range = descriptor.getElementsByTagName("idea-version").item(0).attributes
        check(descriptor.getElementsByTagName("id").item(0).textContent == "top.outlands.minecraft-dev")
        check(range.getNamedItem("since-build").nodeValue.substringBefore('.') == baseline.toString()) {
            "The selected IntelliJ Platform does not match release branch $line"
        }
        check(range.getNamedItem("until-build").nodeValue == "$baseline.*") {
            "A release artifact must cover exactly its own IDE baseline"
        }
    }
}

publishing {
    publications {
        create<MavenPublication>("pluginZip") {
            groupId = "top.outlands"
            artifactId = "minecraft-dev"
            version = currentVersion.get()
            artifact(tasks.buildPlugin.flatMap { it.archiveFile })
            pom {
                name = "Minecraft Development"
                description = "Minecraft Development for IntelliJ-based IDEs"
                url = "https://github.com/Project-Outlands/MinecraftDev"
            }
        }
    }
}

tasks.publishPlugin {
    enabled = false
}
