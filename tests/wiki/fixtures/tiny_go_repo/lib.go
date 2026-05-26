package lib

import "fmt"

type Cache struct {
	items map[string]string
}

type Loader interface {
	Load(path string) ([]byte, error)
}

func (c *Cache) Get(k string) string {
	return c.items[k]
}

func New() *Cache {
	return &Cache{items: make(map[string]string)}
}

func helper() {
	fmt.Println("hello")
}
