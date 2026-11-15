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

package com.demonwav.mcdev.update

import com.intellij.openapi.application.ApplicationInfo
import com.intellij.openapi.updateSettings.impl.UpdateSettingsProvider

class OutlandsRepositoryProvider : UpdateSettingsProvider {
    override fun getPluginRepositories(): List<String> = listOf(repositoryUrl())

    companion object {
        const val REPOSITORY_URL = "https://maven.outlands.top/releases/top/outlands/minecraft-dev"

        fun repositoryUrl(): String {
            val baseline = ApplicationInfo.getInstance().build.baselineVersion
            return "$REPOSITORY_URL/updatePlugins-$baseline.xml"
        }
    }
}
