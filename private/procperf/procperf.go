package procperf

import (
	"github.com/scionproto/scion/pkg/private/serrors"
	"os"
	"sync"
	"time"
)

type Type string

const (
	Received   Type = "Received"
	Propagated Type = "Propagated"
	Originated Type = "Originated"
)

var beaconTime = make(map[string]time.Time)
var file *os.File
var once sync.Once

func Init() error {
	var err error = nil
	once.Do(func() {
		file, _ = os.OpenFile("beacon_time.csv", os.O_CREATE|os.O_RDWR, 0666)
		_, err = file.WriteString("Segment ID; Type; Start Time; End Time\n")
	})
	return err
}

func Close() {
	_ = file.Close()
}

func AddBeaconTime(id string, t time.Time) {
	beaconTime[id] = t
}

func DoneBeacon(id string, procPerfType Type) error {
	if _, ok := beaconTime[id]; ok {
		ppt := string(procPerfType)
		_, err := file.WriteString(id + "; " + ppt + "; " + beaconTime[id].String() + "; " + time.Now().String() + "\n")
		delete(beaconTime, id)
		return err
	} else {
		return serrors.New("beacon not found in beaconTime")
	}
}
