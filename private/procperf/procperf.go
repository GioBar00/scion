package procperf

import (
	"github.com/scionproto/scion/pkg/private/serrors"
	"os"
	"strconv"
	"time"
)

type Type string

const (
	Received   Type = "Received"
	Propagated Type = "Propagated"
	Originated Type = "Originated"
)

var beaconTime = make(map[uint16]time.Time)
var file *os.File

func Init() error {
	file, _ = os.OpenFile("beacon_time.txt", os.O_CREATE|os.O_RDWR, 0666)
	_, err := file.WriteString("Segment ID; Type; Start Time; End Time\n")
	return err
}

func Close() {
	err := file.Close()
	if err != nil {
		return
	}
}

func AddBeaconTime(segmentID uint16, t time.Time) {
	beaconTime[segmentID] = t
}

func DoneBeacon(segmentID uint16, procPerfType Type) error {
	if _, ok := beaconTime[segmentID]; ok {
		sID := strconv.Itoa(int(segmentID))
		ppt := string(procPerfType)
		_, err := file.WriteString(sID + "; " + ppt + "; " + beaconTime[segmentID].String() + "; " + time.Now().String() + "\n")
		delete(beaconTime, segmentID)
		return err
	} else {
		return serrors.New("beacon not found in beaconTime")
	}
}
