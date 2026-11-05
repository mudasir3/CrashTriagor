package com.example.triagedemo;

/** Un-annotated Java API: Kotlin sees its return type as a platform type (String!). */
public class LegacyPrefs {
    public static String getLanguage() {
        return null; // simulates SharedPreferences.getString(key, null) on a fresh install
    }
}
