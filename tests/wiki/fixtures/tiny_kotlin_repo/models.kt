package sample.models

interface Greeter {
    fun greet(name: String): String
}

data class User(val id: Int, var name: String)

sealed class Shape {
    data class Circle(val radius: Double) : Shape()
    object Empty : Shape()
}

enum class Mode { LIGHT, DARK }

class Registry {
    companion object {
        const val VERSION = "1.0"

        fun bootstrap(): String = "boot"
    }
}

val defaultMode: Mode = Mode.LIGHT

fun String.shout(): String = this.uppercase()
