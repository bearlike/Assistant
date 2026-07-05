package sample;

interface Greeter {
    String greet(String name);
}

enum Mode {
    LIGHT, DARK
}

record User(int id, String name) {
}
